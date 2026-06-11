"""Thin client for the PVOG Suchdienst REST API.

All endpoints verified live on 2026-06-10, no API key required.
Quirks: search param is `q`; `size` must be >= 5; JZuFi works via the
versioned v1 paths (the v3 XZuFiVersion header is undocumented/broken).
"""
from __future__ import annotations

import time
import requests

BASE = "https://pvog.fitko.net/suchdienst/api"
UA = {"User-Agent": "Amtsgraph/1.0 (open data pipeline)"}


class PvogClient:
    def __init__(self, base: str = BASE, delay: float = 0.2):
        self.base = base
        self.delay = delay  # be polite: ~5 req/s max
        self.s = requests.Session()
        self.s.headers.update(UA)

    def _get(self, path: str, **params) -> dict | list:
        time.sleep(self.delay)
        r = self.s.get(f"{self.base}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    # -- places ---------------------------------------------------------
    def locations(self, q: str) -> list[dict]:
        """Place search: returns [{ars, ags, name, zip, zipCodeCount, ...}]."""
        return self._get("/v2/locations", q=q)

    def location_suggestions(self, q: str) -> list[dict]:
        return self._get("/v2/locations/suggestions", q=q)

    # -- services (Leistungen) ------------------------------------------
    def services(self, ars: str, q: str | None = None, size: int = 20,
                 page: int = 0) -> dict:
        """Services available at a place. size must be >= 5."""
        params = {"size": max(size, 5), "page": page}
        if q:
            params["q"] = q
        return self._get(f"/v7/servicedescriptions/{ars}", **params)

    def services_by_leika(self, leika_id: str, size: int = 20) -> dict:
        return self._get("/v3/servicedescriptions/leikaid",
                         leikaid=leika_id, size=max(size, 5))

    # -- responsible authorities ----------------------------------------
    def responsible_units(self, ars: str, lb_id: str) -> list[dict]:
        """Who is competent for service lb_id at place ars.

        Returns [{id: 'L100042.OE.2292', title: 'Landratsamt Donau-Ries',
                  role: {...}}].
        """
        return self._get("/v2/organisationunits/titles", ars=ars, lbId=lb_id)

    def unit_detail(self, oe_id: str) -> dict:
        """Full record: addresses (with geo), email, web, opening hours."""
        return self._get("/v5/organisationunits/detail", q=oe_id)

    # -- justice competence finder --------------------------------------
    def jzufi_relations(self, ars: str, lb_ids: list[str]) -> dict:
        return self._get("/v1/relations/jzufi-2-3", ars=ars, lbids=lb_ids)
