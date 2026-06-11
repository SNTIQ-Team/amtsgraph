"""Harvest court competence chains from the official Orts- und
Gerichtsverzeichnis (justizadressen.nrw.de) — plain HTTP, no Selenium.

Two phases (both parallel and resumable):

1. Place register: GET /de/justiz/orte?plzort=<PLZ>&filter=gericht for every
   PLZ in the OpenPLZ snapshot. One PLZ can hold many Orte in *different*
   court districts (PLZ 25712 -> 9 Orte, 9 gs keys), so the resolution unit
   is (plz, ortk). Each entry carries the structural court key -> gs_key.

2. Chains: places sharing a gs_key share courts, so fetch
   GET /de/justiz/gericht?plz=&ort=&ang=<matter> once per (gs_key, matter),
   parsing the full instance chain (court cards with both addresses,
   phone/fax/email/web, XJustiz-ID, ERV status). A random sample of
   non-representative places is re-fetched and compared — if the gs_key
   assumption ever breaks, the run fails loudly.

Output (data/snapshots/justiz/<date>/): places.jsonl, chains.jsonl (append,
resumable), hinweise.jsonl, verify.jsonl.

Usage:
    python fetch_justiz.py --phase places [--plz-prefix 86] [--workers 6]
    python fetch_justiz.py --phase chains [--matters zivil,mahn] [--workers 6]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

from common import HttpPool, latest_snapshot, read_jsonl, snapshot_dir, write_jsonl

BASE = "https://www.justizadressen.nrw.de/de/justiz"

CORE_MATTERS = ["zivil", "familie", "mahn", "insolv", "insolvver", "zvg",
                "zwangsvoll", "grundbuch", "nachlass", "betreu", "handelsreg",
                "arbeit", "sozial", "verwaltung"]

PROSECUTION_RE = re.compile(r"staatsanwaltschaft", re.I)

# every result page carries this navigational hint; not an ambiguity warning
BOILERPLATE_HINWEIS = re.compile(
    r"Für die folgenden Angelegenheiten|Auswahlhilfe", re.I)


# ----------------------------------------------------------------- parsing

def parse_chain(html: str) -> tuple[list[dict], list[str]]:
    """Parse a /gericht result page into ordered authority cards + Hinweise."""
    soup = BeautifulSoup(html, "html.parser")
    cards, hinweise = [], []

    for hint in soup.find_all(string=re.compile(r"^\s*Hinweis")):
        block = hint.find_parent(["p", "div"])
        if block:
            text = " ".join(block.get_text(" ", strip=True).split())
            if len(text) > 12 and not BOILERPLATE_HINWEIS.search(text):
                hinweise.append(text)

    for h6 in soup.find_all("h6"):
        small = h6.find("small")
        note = " ".join(small.get_text(" ", strip=True).split()) if small else None
        if small:
            small.extract()
        name = " ".join(h6.get_text(" ", strip=True).split())
        if not name or len(name) < 4:
            continue
        card = {"name": name, "role": "prosecution"
                if PROSECUTION_RE.search(name) else "court"}
        if note:
            card["note"] = note
        block = h6.find_next_sibling("div")
        if not block:
            cards.append(card)
            continue

        for addr in block.find_all("address"):
            text_lines = addr.get_text("\n", strip=True)
            label = addr.find("strong")
            key = label.get_text(strip=True) if label else ""
            plain = " ".join(
                text_lines.replace(key, "", 1).split()).strip(", ")
            if key.startswith("Liefer"):
                card["address"] = plain
            elif key.startswith("Post"):
                card["postal_address"] = plain
            elif key.startswith("Kontakt"):
                for pat, field in ((r"Telefon:\s*([^\n]+)", "phone"),
                                   (r"(?:Tele)?[Ff]ax:\s*([^\n]+)", "fax")):
                    m = re.search(pat, text_lines)
                    if m:
                        card[field] = m.group(1).strip()
                mail = addr.find("a", href=re.compile(r"^mailto:.+"))
                if mail:
                    card["email"] = mail["href"][7:]
                web = addr.find("a", href=re.compile(r"^https?://"))
                if web:
                    card["web"] = web["href"]
            else:
                m = re.search(r"XJustiz-ID:\s*([A-Z0-9]+)", text_lines)
                if m:
                    card["xjustiz_id"] = m.group(1)

        block_text = block.get_text("\n", strip=True)
        m = re.search(r"Der elektronische Rechtsverkehr[^\n]*", block_text)
        if m:
            card["erv_note"] = " ".join(m.group(0).split())
        else:
            p = block.find("p", string=re.compile(r"Elektronischer Rechtsverkehr"))
            if p:
                ul = p.find_next_sibling("ul")
                items = [li.get_text(strip=True)
                         for li in (ul.find_all("li") if ul else [])]
                card["erv_note"] = ("Elektronischer Rechtsverkehr in: "
                                    + ", ".join(i for i in items if i))
        cards.append(card)
    return cards, hinweise


# --------------------------------------------------------------- phase 1

def fetch_places(http: HttpPool, plz_list: list[str], out: Path,
                 workers: int) -> None:
    done_plz: set[str] = set()
    places_path = out / "places.jsonl"
    if places_path.exists():                       # resume
        done_plz = {r["plz"] for r in read_jsonl(places_path)}
        plz_list = [p for p in plz_list if p not in done_plz]
    lock = threading.Lock()
    counter = {"n": 0}

    def work(plz: str) -> tuple[str, list[dict], str | None]:
        try:
            r = http.get(f"{BASE}/orte",
                         params={"plzort": plz, "filter": "gericht"})
            entries = r.json()
        except Exception as exc:                   # noqa: BLE001
            return plz, [], str(exc)
        rows = [{
            "plz": e["plz"], "ortk": e["ortk"], "ort": e["ort"],
            "plzm": e.get("plzm"), "gebm": e.get("gebm"),
            "gs_key": "|".join((e.get("gsbl") or "", e.get("gsregbez") or "",
                                e.get("gskreis") or "", e.get("gsort") or "")),
        } for e in entries if e.get("plz") == plz]
        return plz, rows, None if rows else "no-entry"

    with open(places_path, "a", encoding="utf-8") as pf, \
         open(out / "places_misses.jsonl", "a", encoding="utf-8") as mf, \
         ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed(ex.submit(work, p) for p in plz_list):
            plz, rows, err = fut.result()
            with lock:
                for row in rows:
                    pf.write(json.dumps(row, ensure_ascii=False) + "\n")
                if err:
                    mf.write(json.dumps({"plz": plz, "error": err}) + "\n")
                counter["n"] += 1
                if counter["n"] % 500 == 0:
                    pf.flush()
                    print(f"  places: {counter['n']}/{len(plz_list)} PLZ")
    print(f"places done (+{counter['n']} PLZ this run)")


# --------------------------------------------------------------- phase 2

def fetch_chain(http: HttpPool, plz: str, ort: str, matter: str):
    r = http.get(f"{BASE}/gericht",
                 params={"plz": plz, "ort": ort, "ang": matter})
    if "keinen Treffer" in r.text:
        return None, []
    return parse_chain(r.text)


def chain_signature(cards: list[dict]) -> str:
    return ";".join(f"{c['role']}:{c.get('xjustiz_id') or c['name']}"
                    for c in cards or [])


def fetch_chains(http: HttpPool, places: list[dict], matters: list[str],
                 sample_size: int, out: Path, workers: int,
                 reverse: bool = False, sfx: str = "") -> int:
    by_gs: dict[str, list[dict]] = {}
    for p in places:
        by_gs.setdefault(p["gs_key"], []).append(p)

    chains_path = out / f"chains{sfx}.jsonl"
    done: set[tuple[str, str]] = set()
    for f in out.glob("chains*.jsonl"):            # resume across all processes
        done |= {(c["gs_key"], c["matter"]) for c in read_jsonl(f)}

    tasks = [(gs, members[0], m) for gs, members in sorted(by_gs.items())
             for m in matters if (gs, m) not in done]
    if reverse:
        tasks.reverse()
    print(f"{len(places)} places -> {len(by_gs)} gs keys; "
          f"{len(tasks)} (gs,matter) tasks remaining "
          f"({len(done)} already done)")

    lock = threading.Lock()
    counter = {"n": 0, "err": 0}

    def work(task):
        gs, rep, matter = task
        try:
            cards, hinweise = fetch_chain(http, rep["plz"], rep["ort"], matter)
        except Exception as exc:                   # noqa: BLE001
            return gs, matter, rep, None, [], str(exc)
        return gs, matter, rep, cards, hinweise, None

    with open(chains_path, "a", encoding="utf-8") as cf, \
         open(out / f"hinweise{sfx}.jsonl", "a", encoding="utf-8") as hf, \
         open(out / f"chain_errors{sfx}.jsonl", "a", encoding="utf-8") as ef, \
         ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed(ex.submit(work, t) for t in tasks):
            gs, matter, rep, cards, hinweise, err = fut.result()
            with lock:
                if err:
                    counter["err"] += 1
                    ef.write(json.dumps({"gs_key": gs, "matter": matter,
                                         "error": err}) + "\n")
                else:
                    cf.write(json.dumps(
                        {"gs_key": gs, "matter": matter,
                         "rep_plz": rep["plz"], "rep_ort": rep["ort"],
                         "cards": cards or []}, ensure_ascii=False) + "\n")
                    for h in hinweise:
                        hf.write(json.dumps(
                            {"gs_key": gs, "matter": matter,
                             "rep_plz": rep["plz"], "text": h},
                            ensure_ascii=False) + "\n")
                counter["n"] += 1
                if counter["n"] % 500 == 0:
                    cf.flush()
                    print(f"  chains: {counter['n']}/{len(tasks)} "
                          f"({counter['err']} errors)")
    print(f"chains done: {counter['n']} fetched, {counter['err']} errors")

    # --- spot-verify the gs_key assumption -----------------------------
    chain_by_key = {}
    for f in out.glob("chains*.jsonl"):
        for c in read_jsonl(f):
            chain_by_key[(c["gs_key"], c["matter"])] = chain_signature(c["cards"])
    candidates = [p for p in places if len(by_gs[p["gs_key"]]) > 1
                  and p != by_gs[p["gs_key"]][0]]
    sample = random.sample(candidates, min(sample_size, len(candidates)))
    results, mismatches = [], 0
    for p in sample:
        matter = random.choice(matters)
        if (p["gs_key"], matter) not in chain_by_key:
            continue
        try:
            cards, _ = fetch_chain(http, p["plz"], p["ort"], matter)
        except Exception:                          # noqa: BLE001
            continue
        ok = chain_signature(cards) == chain_by_key[(p["gs_key"], matter)]
        mismatches += (not ok)
        results.append({"plz": p["plz"], "ort": p["ort"], "matter": matter,
                        "ok": ok, "got": chain_signature(cards),
                        "expected": chain_by_key[(p["gs_key"], matter)]})
    write_jsonl(out / f"verify{sfx}.jsonl", results)
    print(f"spot-check: {len(results)} sampled, {mismatches} mismatches")
    if mismatches:
        print("FATAL: gs_key sharing assumption violated — do NOT build from "
              "this snapshot; investigate verify.jsonl", file=sys.stderr)
        return 1
    return 0


def fetch_pchains(http: HttpPool, places: list[dict], matters: list[str],
                  out: Path, workers: int, reverse: bool, sfx: str) -> int:
    """Per-place harvest — NO grouping assumption. The gs_key grouping was
    disproven nationwide (PLZ 10115 and 12555 share a gs tuple but resolve
    to different Amtsgerichte): the portal resolves by the PLZ itself, so
    every (plz, ortk, matter) is fetched directly."""
    done: set[tuple[str, str, str]] = set()
    for f in out.glob("pchains*.jsonl"):
        done |= {(c["plz"], c["ortk"], c["matter"]) for c in read_jsonl(f)}
    tasks = [(p, m) for p in places for m in matters
             if (p["plz"], p["ortk"], m) not in done]
    if reverse:
        tasks.reverse()
    print(f"{len(places)} places x {len(matters)} matters: "
          f"{len(tasks)} tasks remaining ({len(done)} done)")

    lock = threading.Lock()
    counter = {"n": 0, "err": 0}

    def work(task):
        p, matter = task
        try:
            cards, hinweise = fetch_chain(http, p["plz"], p["ort"], matter)
            return p, matter, cards, hinweise, None
        except Exception as exc:                   # noqa: BLE001
            return p, matter, None, [], str(exc)

    with open(out / f"pchains{sfx}.jsonl", "a", encoding="utf-8") as cf, \
         open(out / f"hinweise{sfx}.jsonl", "a", encoding="utf-8") as hf, \
         open(out / f"chain_errors{sfx}.jsonl", "a", encoding="utf-8") as ef, \
         ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed(ex.submit(work, t) for t in tasks):
            p, matter, cards, hinweise, err = fut.result()
            with lock:
                if err:
                    counter["err"] += 1
                    ef.write(json.dumps({"plz": p["plz"], "ortk": p["ortk"],
                                         "matter": matter, "error": err}) + "\n")
                else:
                    cf.write(json.dumps(
                        {"plz": p["plz"], "ortk": p["ortk"], "ort": p["ort"],
                         "matter": matter, "cards": cards or []},
                        ensure_ascii=False) + "\n")
                    for h in hinweise:
                        hf.write(json.dumps(
                            {"plz": p["plz"], "ortk": p["ortk"],
                             "matter": matter, "text": h},
                            ensure_ascii=False) + "\n")
                counter["n"] += 1
                if counter["n"] % 500 == 0:
                    cf.flush()
                    print(f"  pchains: {counter['n']}/{len(tasks)} "
                          f"({counter['err']} errors)")
    print(f"pchains done: {counter['n']}, errors {counter['err']}")
    return 0


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["places", "chains", "pchains"],
                    required=True)
    ap.add_argument("--plz-prefix", help="limit phase 1 to a PLZ prefix")
    ap.add_argument("--matters", help="comma list, default core set")
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--reverse", action="store_true",
                    help="process tasks from the end (helper process)")
    ap.add_argument("--suffix", default="",
                    help="output file suffix so concurrent processes never "
                         "interleave writes in one file")
    ap.add_argument("--delay", type=float, default=0.25,
                    help="per-worker delay; effective rate ~ workers/delay")
    args = ap.parse_args()

    http = HttpPool(delay=args.delay)
    # reuse the running/last harvest dir (a run crossing midnight must not
    # silently switch to a fresh day directory)
    out = latest_snapshot("justiz") or snapshot_dir("justiz")

    if args.phase == "places":
        src = latest_snapshot("openplz")
        if not src:
            print("run fetch_openplz.py first", file=sys.stderr)
            return 1
        plz = sorted({r["postalCode"]
                      for r in read_jsonl(src / "localities.jsonl")})
        if args.plz_prefix:
            plz = [p for p in plz if p.startswith(args.plz_prefix)]
        print(f"fetching places for {len(plz)} PLZ "
              f"({args.workers} workers)...")
        fetch_places(http, plz, out, args.workers)
        return 0

    places_file = out / "places.jsonl"
    if not places_file.exists():
        print("run --phase places first", file=sys.stderr)
        return 1
    # dedupe places (resume runs may append duplicates)
    seen, places = set(), []
    for p in read_jsonl(places_file):
        k = (p["plz"], p["ortk"])
        if k not in seen:
            seen.add(k)
            places.append(p)
    matters = (args.matters.split(",") if args.matters else CORE_MATTERS)
    sfx = f"_{args.suffix}" if args.suffix else ""
    if args.phase == "pchains":
        return fetch_pchains(http, places, matters, out, args.workers,
                             args.reverse, sfx)
    return fetch_chains(http, places, matters, args.sample, out, args.workers,
                        reverse=args.reverse, sfx=sfx)


if __name__ == "__main__":
    sys.exit(main())
