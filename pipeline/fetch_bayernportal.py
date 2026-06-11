"""Harvest the FULL organisational web of Bavaria's Kreisverwaltungsbehörden
from BayernPortal: every Abteilung/Fachbereich/Sachgebiet with its
parent-child structure ("supervisory web"), contacts per unit.

Roots: Behördenordner 8111031172 ("Kreisverwaltungsbehörden") lists all
71 Landratsämter + 25 kreisfreie Städte in one page. Each root has a
public organigram at /dokumente/behoerde/<id>/organigramm rendered as a
nested collapsible tree:

    li.collapsible-list-leaf  -> unit with own document (a[href] + aria-label)
    li.collapsible-list-node  -> grouping (span.collapsible-list-title),
                                 children in ul.collapsible-list-children;
                                 may or may not have an own document

Output (data/snapshots/bayernportal/<date>/):
    roots.jsonl  {root_id, name}
    units.jsonl  {key, oe_id|null, parent_key, root_id, root_name, name,
                  depth, address?, postal_address?, phone?, fax?, email?}

Usage:
    python fetch_bayernportal.py [--workers 6] [--delay 0.25] [--limit-roots N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup

from common import HttpPool, latest_snapshot, read_jsonl, snapshot_dir, write_jsonl

BASE = "https://www.bayernportal.de"
ORDNER_KVB = "8111031172"            # Kreisverwaltungsbehörden: 71 LRA + 25 Städte

DOC_RE = re.compile(r"/dokumente/behoerde/(\d+)")


def soup_of(http: HttpPool, path: str) -> BeautifulSoup | None:
    r = http.get(f"{BASE}{path}", timeout=30)
    if r.status_code != 200:
        return None
    return BeautifulSoup(r.text, "html.parser")


def fetch_roots(http: HttpPool) -> list[dict]:
    soup = soup_of(http, f"/dokumente/behoerdeordner/{ORDNER_KVB}")
    roots = {}
    for a in soup.find_all("a", href=DOC_RE):
        oid = DOC_RE.search(a["href"]).group(1)
        name = " ".join(a.get_text(" ", strip=True).split())
        if name and len(name) > 3 and oid not in roots:
            roots[oid] = name
    return [{"root_id": k, "name": v} for k, v in sorted(roots.items())]


# ------------------------------------------------------------- tree parse

def li_own_link(li, child_ul):
    """First behoerde link inside li that is NOT inside its child subtree."""
    for a in li.find_all("a", href=DOC_RE):
        if child_ul is not None and child_ul in a.parents:
            continue
        return a
    return None


def walk(ul, parent_key: str | None, depth: int, root_id: str,
         rows: list[dict], counter: list[int]):
    for li in ul.find_all("li", recursive=False):
        child_ul = li.find("ul", recursive=False)
        a = li_own_link(li, child_ul)
        if a is not None:
            oid = DOC_RE.search(a["href"]).group(1)
            name = a.get("aria-label") or a.get_text(" ", strip=True)
        else:
            oid = None
            t = li.find("span", class_="collapsible-list-title")
            name = t.get_text(" ", strip=True) if t else None
        name = " ".join((name or "").split())
        if name and oid != root_id:
            counter[0] += 1
            key = oid or f"synth:{root_id}:{counter[0]}"
            rows.append({"key": key, "oe_id": oid, "parent_key": parent_key,
                         "depth": depth, "name": name})
            next_parent = key
            next_depth = depth + 1
        else:                                    # root echo / unnamed wrapper
            next_parent = parent_key
            next_depth = depth
        if child_ul is not None:
            walk(child_ul, next_parent, next_depth, root_id, rows, counter)


def parse_tree(soup: BeautifulSoup, root_id: str) -> list[dict]:
    candidates = [ul for ul in soup.find_all("ul")
                  if ul.find("li", class_=re.compile("collapsible-list"),
                             recursive=False)]
    outer = [ul for ul in candidates
             if not any(ul is not other and other in ul.parents
                        for other in candidates)]
    rows: list[dict] = []
    counter = [0]
    for ul in outer:
        walk(ul, None, 0, root_id, rows, counter)
    return rows


FIELD_RE = {
    "phone": re.compile(r"Telefon\n([+\d][^\n]*)"),
    "fax": re.compile(r"(?:Tele)?[Ff]ax\n([+\d][^\n]*)"),
    "email": re.compile(r"E-Mail\n([^\s@]+@[^\s]+)"),
}
ADDR_RE = re.compile(
    r"(Hausanschrift|Postanschrift)\n((?:[^\n]+\n)?[^\n]*\d{5}[^\n]*)")


def parse_detail(soup: BeautifulSoup) -> dict:
    text = soup.get_text("\n", strip=True)
    d: dict = {}
    for label, addr in ADDR_RE.findall(text):
        key = "address" if label == "Hausanschrift" else "postal_address"
        d.setdefault(key, " ".join(addr.split("\n")).strip())
    for field, rx in FIELD_RE.items():
        m = rx.search(text)
        if m:
            d[field] = m.group(1).strip()
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--delay", type=float, default=0.25)
    ap.add_argument("--limit-roots", type=int)
    args = ap.parse_args()

    http = HttpPool(delay=args.delay)
    out = latest_snapshot("bayernportal") or snapshot_dir("bayernportal")

    roots_path = out / "roots.jsonl"
    if roots_path.exists():
        roots = list(read_jsonl(roots_path))
    else:
        roots = fetch_roots(http)
        write_jsonl(roots_path, roots)
    print(f"roots: {len(roots)} Kreisverwaltungsbehörden")
    if args.limit_roots:
        roots = roots[:args.limit_roots]

    units_path = out / "units.jsonl"
    done_roots = ({u["root_id"] for u in read_jsonl(units_path)}
                  if units_path.exists() else set())
    todo = [r for r in roots if r["root_id"] not in done_roots]
    print(f"{len(todo)} roots to harvest ({len(done_roots)} done)")

    lock = threading.Lock()
    total = [0]

    def work(root: dict):
        rid = root["root_id"]
        tree_soup = soup_of(http, f"/dokumente/behoerde/{rid}/organigramm")
        if tree_soup is None:
            return root, [], "no-organigramm"
        units = parse_tree(tree_soup, rid)
        for u in units:
            u["root_id"] = rid
            u["root_name"] = root["name"]
            if u["oe_id"]:
                d = soup_of(http, f"/dokumente/behoerde/{u['oe_id']}")
                if d is not None:
                    u.update(parse_detail(d))
        return root, units, None

    with open(units_path, "a", encoding="utf-8") as uf, \
         open(out / "misses.jsonl", "a", encoding="utf-8") as mf, \
         ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in as_completed(ex.submit(work, r) for r in todo):
            root, units, err = fut.result()
            with lock:
                if err:
                    mf.write(json.dumps({"root": root, "error": err}) + "\n")
                for u in units:
                    uf.write(json.dumps(u, ensure_ascii=False) + "\n")
                uf.flush()
                total[0] += len(units)
                print(f"  {root['name']}: {len(units)} OE (total {total[0]})")
    print(f"snapshot -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
