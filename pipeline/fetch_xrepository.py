"""Fetch authoritative identity codelists from xRepository (genericode XML).

These are COMPLETE official registries — used as completeness benchmarks
for our harvested data (validate: every XJustiz-ID we serve must exist in
GDS.Gerichte; ABH coverage can be measured against ABH-Kennung).

- GDS.Gerichte: all courts, prosecutions, justice authorities + XJustiz-IDs
- ABH-Kennung:  all valid Ausländerbehörden (BAMF codelist)

Output (data/snapshots/xrepository/<date>/): gerichte.jsonl, abh.jsonl
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

from common import Http, snapshot_dir, write_jsonl

GC_NS = "{http://docs.oasis-open.org/codelist/ns/genericode/1.0/}"
BASE = "https://www.xrepository.de/api/codeliste"

LISTS = {
    "gerichte": "urn:xoev-de:xjustiz:codeliste:gds.gerichte",
    "abh": "urn:de:xauslaender:codelist:abhkennung",
}


def fetch_genericode(http: Http, urn: str) -> tuple[str, list[dict]]:
    meta = http.get(f"{BASE}/{urn}/gueltigeVersion",
                    headers={"Accept": "application/json"}).json()
    version_urn = meta["kennung"]
    xml = http.get(f"https://www.xrepository.de/api/version_codeliste/"
                   f"{version_urn}/genericode", timeout=60).content
    root = ET.fromstring(xml)
    # genericode: the root carries the gc namespace, child elements are
    # UNQUALIFIED — search both variants to be schema-agnostic
    def findall(node, path):
        return node.findall(path) or node.findall(
            "/".join(GC_NS + p for p in path.split("/")).replace(
                GC_NS + ".", "."))
    rows = []
    for row in findall(root, ".//SimpleCodeList/Row"):
        rec = {}
        for v in findall(row, "Value"):
            ref = v.attrib.get("ColumnRef")
            val = v.find("SimpleValue")
            if val is None:
                val = v.find(f"{GC_NS}SimpleValue")
            rec[ref] = val.text if val is not None else None
        rows.append(rec)
    return version_urn, rows


def main() -> int:
    http = Http(delay=0.3)
    out = snapshot_dir("xrepository")
    for name, urn in LISTS.items():
        version, rows = fetch_genericode(http, urn)
        write_jsonl(out / f"{name}.jsonl", rows)
        print(f"{name}: {len(rows)} rows ({version})")
    print(f"snapshot -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
