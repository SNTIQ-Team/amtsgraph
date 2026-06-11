"""Fetch the GEO spine from OpenPLZ API into a snapshot.

Output (data/snapshots/openplz/<date>/):
    federal_states.jsonl, districts.jsonl, municipalities.jsonl, localities.jsonl

Usage:
    python fetch_openplz.py            # whole Germany
    python fetch_openplz.py --land 09  # one federal state (pilot runs)
"""
from __future__ import annotations

import argparse
import sys

from common import Http, snapshot_dir, write_jsonl

BASE = "https://openplzapi.org/de"
PAGE_SIZE = 50  # API maximum


def paged(http: Http, path: str, **params):
    page = 1
    while True:
        r = http.get(f"{BASE}/{path}",
                     params={**params, "page": page, "pageSize": PAGE_SIZE})
        if r.status_code == 404:        # past last page
            return
        r.raise_for_status()
        items = r.json()
        if not items:
            return
        yield from items
        if len(items) < PAGE_SIZE:
            return
        page += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--land", help="federal state key, e.g. 09 (default: all)")
    args = ap.parse_args()

    http = Http(delay=0.12)
    out = snapshot_dir("openplz")

    states = list(paged(http, "FederalStates"))
    if args.land:
        states = [s for s in states if s["key"] == args.land]
        if not states:
            print(f"unknown land key {args.land}", file=sys.stderr)
            return 1
    write_jsonl(out / "federal_states.jsonl", states)
    print(f"federal_states: {len(states)}")

    districts, municipalities, localities = [], [], []
    for st in states:
        key = st["key"]
        d = list(paged(http, f"FederalStates/{key}/Districts"))
        m = list(paged(http, f"FederalStates/{key}/Municipalities"))
        l = list(paged(http, f"FederalStates/{key}/Localities"))
        districts += d
        municipalities += m
        localities += l
        print(f"  {st['name']}: {len(d)} districts, {len(m)} municipalities, "
              f"{len(l)} localities")

    write_jsonl(out / "districts.jsonl", districts)
    write_jsonl(out / "municipalities.jsonl", municipalities)
    write_jsonl(out / "localities.jsonl", localities)
    print(f"snapshot -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
