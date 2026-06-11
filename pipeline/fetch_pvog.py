"""Harvest non-court authorities from the PVOG Suchdienst API.

For every Gemeinde (from the OpenPLZ snapshot) and every authority kind in
pvog_leika.yaml: search the probe service at the Gemeinde's ARS, ask PVOG
which Organisationseinheit is zuständig, and fetch its full record once.

ARS values come from PVOG's own /v2/locations index (queried by PLZ), since
OpenPLZ only carries the 8-digit AGS, not the 12-digit ARS.

Resumable: already-processed AGS are skipped on re-run (state in done.jsonl).

Output (data/snapshots/pvog/<date>/):
    competences.jsonl  {ags, ars, kind, oe_id, oe_title, probe_q, lb_id}
    units.jsonl        full OE records (one per oe_id)
    done.jsonl         processed AGS (resume state)
    misses.jsonl       gemeinden where no OE was found for a kind

Usage:
    python fetch_pvog.py [--land 09] [--kinds auslaenderbehoerde,jobcenter]
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import yaml

from common import Http, HttpPool, latest_snapshot, read_jsonl, snapshot_dir, ROOT

BASE = "https://pvog.fitko.net/suchdienst/api"


def get_json(http: Http, path: str, **params):
    r = http.get(f"{BASE}{path}", params=params)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except json.JSONDecodeError:
        return None


def ars_for_plz(http: Http, plz: str, ags: str) -> str | None:
    locs = get_json(http, "/v2/locations", q=plz) or []
    for loc in locs:
        if loc.get("ags") == ags:
            return loc.get("ars")
    return None


# federal call centers etc. that PVOG sometimes lists as Ansprechpunkt;
# never the locally competent office
GLOBAL_DENY = re.compile(r"hotline|servicecenter|service-center", re.I)


def find_units(http: Http, ars: str, probe: dict,
               collect_all: bool) -> list[tuple[dict, str]]:
    """Search probe service at ars, return [(unit_title_row, lb_id), ...].

    Service titles are often empty in the v7 listing, so must_match runs
    against title+description. With collect_all every competent unit is
    returned (heterogeneous kinds like AsylbLG); otherwise the first hit.
    """
    res = get_json(http, f"/v7/servicedescriptions/{ars}",
                   q=probe["q"], size=10)
    if not res:
        return []
    must = re.compile(probe["must_match"], re.I)
    deny = re.compile(probe["deny"], re.I) if probe.get("deny") else None
    prefer = re.compile(probe["prefer"], re.I) if probe.get("prefer") else None
    # positive unit-title requirement: stronger than deny-list whack-a-mole —
    # for kinds where wrong offices keep surfacing (gE Jobcenter gaps),
    # only plausibly-named units are accepted at all; rest = honest miss
    require = re.compile(probe["require"], re.I) if probe.get("require") else None
    found: list[tuple[dict, str]] = []
    seen_oe: set[str] = set()
    for svc in (res.get("serviceDescriptions") or {}).get("content", []):
        haystack = f"{svc.get('title') or ''} {svc.get('description') or ''}"
        if haystack.strip() and not must.search(haystack):
            continue
        units = get_json(http, "/v2/organisationunits/titles",
                         ars=ars, lbId=svc["id"]) or []
        for u in units:
            role = (u.get("role") or {}).get("code")
            unit_title = u.get("title") or ""
            if role not in ("01", "02", "03"):
                continue
            if GLOBAL_DENY.search(unit_title) or (deny and deny.search(unit_title)):
                continue
            if require and not require.search(unit_title):
                continue
            if u["id"] in seen_oe:
                continue
            seen_oe.add(u["id"])
            found.append((u, svc["id"]))
    if prefer:  # preferred unit titles first, otherwise keep discovery order
        found.sort(key=lambda f: 0 if prefer.search(f[0].get("title") or "") else 1)
    return found if collect_all else found[:1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--land", help="land key filter, e.g. 09")
    ap.add_argument("--kinds", help="comma list, default: all in pvog_leika.yaml")
    ap.add_argument("--delay", type=float, default=0.25,
                    help="per-worker delay; effective rate ~ workers/delay")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--reverse", action="store_true",
                    help="process gemeinden from the end (second helper "
                         "process meets the first in the middle)")
    ap.add_argument("--suffix", default="",
                    help="output file suffix so concurrent processes never "
                         "interleave writes in one file")
    ap.add_argument("--shard", default="",
                    help="K/N: process only gemeinden with index %% N == K "
                         "(stable split for N concurrent processes)")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(ROOT / "pipeline" / "pvog_leika.yaml"))["kinds"]
    kinds = {k: v for k, v in cfg.items()
             if not args.kinds or k in args.kinds.split(",")}

    src = latest_snapshot("openplz")
    if not src:
        print("run fetch_openplz.py first", file=sys.stderr)
        return 1
    gemeinden = [m for m in read_jsonl(src / "municipalities.jsonl")
                 if not args.land or m["key"].startswith(args.land)]
    # one representative PLZ per gemeinde for the ARS lookup
    plz_by_ags: dict[str, str] = {}
    for loc in read_jsonl(src / "localities.jsonl"):
        plz_by_ags.setdefault(loc["municipality"]["key"], loc["postalCode"])
    # city-states (Berlin, Hamburg) are absent from the Municipalities
    # route — synthesize them from the localities, or they are silently
    # skipped for ALL kinds (zero Behörden for 5.5M people)
    known = {m["key"] for m in gemeinden}
    seen_extra = set()
    for loc in read_jsonl(src / "localities.jsonl"):
        k = loc["municipality"]["key"]
        if (k not in known and k not in seen_extra
                and (not args.land or k.startswith(args.land))):
            seen_extra.add(k)
            gemeinden.append({"key": k,
                              "name": loc["municipality"].get("name")
                              or loc["name"],
                              "postalCode": loc["postalCode"]})

    # reuse the running harvest's dir across midnight instead of starting
    # a fresh day directory
    out = latest_snapshot("pvog") or snapshot_dir("pvog")
    sfx = f"_{args.suffix}" if args.suffix else ""
    done_path = out / f"done{sfx}.jsonl"
    done = set()
    for f in out.glob("done*.jsonl"):           # union across all processes
        done |= {r["ags"] for r in read_jsonl(f)}

    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    http = HttpPool(delay=args.delay)
    # resume: also skip units already detailed
    units_path = out / f"units{sfx}.jsonl"
    seen_units: set[str] = set()
    for f in out.glob("units*.jsonl"):
        seen_units |= {u["oe_id"] for u in read_jsonl(f)}
    todo = [g for g in gemeinden if g["key"] not in done]
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        todo.sort(key=lambda g: g["key"])
        todo = [g for i, g in enumerate(todo) if i % n == k]
    if args.reverse:
        todo.reverse()
    print(f"{len(todo)} gemeinden to process ({len(done)} already done), "
          f"{args.workers} workers")

    lock = threading.Lock()
    counter = {"n": 0}

    def work(g: dict) -> tuple[str, list[dict], list[dict], list[dict]]:
        """Returns (ags, competence_rows, unit_rows, miss_rows)."""
        ags = g["key"]
        comps, units, misses = [], [], []
        plz = plz_by_ags.get(ags) or g.get("postalCode")
        ars = ars_for_plz(http, plz, ags) if plz else None
        if not ars:
            return ags, [], [], [{"ags": ags, "error": "no-ars"}]
        for kind, spec in kinds.items():
            multi = isinstance(spec, dict) and spec.get("multi", False)
            probes = spec["probes"] if isinstance(spec, dict) else spec
            found: list[tuple[dict, str]] = []
            for probe in probes:
                hits = find_units(http, ars, probe, collect_all=multi)
                for h in hits:
                    if h[0]["id"] not in {f[0]["id"] for f in found}:
                        found.append(h)
                if found and not multi:
                    break
            if not found:
                misses.append({"ags": ags, "kind": kind, "error": "no-unit"})
                continue
            for unit, lb_id in found:
                comps.append({
                    "ags": ags, "ars": ars, "kind": kind,
                    "oe_id": unit["id"], "oe_title": unit.get("title"),
                    "lb_id": lb_id,
                    "ambiguous": multi and len(found) > 1})
                with lock:
                    if unit["id"] in seen_units:
                        continue
                    seen_units.add(unit["id"])
                detail = get_json(http, "/v5/organisationunits/detail",
                                  q=unit["id"])
                if detail:
                    units.append({"oe_id": unit["id"], "detail": detail})
        return ags, comps, units, misses

    with open(out / f"competences{sfx}.jsonl", "a", encoding="utf-8") as comp_f, \
         open(units_path, "a", encoding="utf-8") as units_f, \
         open(out / f"misses{sfx}.jsonl", "a", encoding="utf-8") as miss_f, \
         open(done_path, "a", encoding="utf-8") as done_f, \
         ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in as_completed(ex.submit(work, g) for g in todo):
            ags, comps, units, misses = fut.result()
            with lock:
                for c in comps:
                    comp_f.write(json.dumps(c, ensure_ascii=False) + "\n")
                for u in units:
                    units_f.write(json.dumps(u, ensure_ascii=False) + "\n")
                for m in misses:
                    miss_f.write(json.dumps(m, ensure_ascii=False) + "\n")
                done_f.write(json.dumps({"ags": ags}) + "\n")
                counter["n"] += 1
                if counter["n"] % 100 == 0:
                    for f in (comp_f, units_f, miss_f, done_f):
                        f.flush()
                    print(f"  {counter['n']}/{len(todo)} gemeinden")
    print(f"snapshot -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
