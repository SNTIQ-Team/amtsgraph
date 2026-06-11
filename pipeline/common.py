"""Shared pipeline utilities: snapshots, normalization, polite HTTP."""
from __future__ import annotations

import json
import time
import unicodedata
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = ROOT / "data" / "snapshots"


def snapshot_dir(source: str, day: str | None = None) -> Path:
    d = SNAPSHOTS / source / (day or date.today().isoformat())
    d.mkdir(parents=True, exist_ok=True)
    return d


def latest_snapshot(source: str) -> Path | None:
    base = SNAPSHOTS / source
    if not base.is_dir():
        return None
    days = sorted(p for p in base.iterdir() if p.is_dir())
    return days[-1] if days else None


def write_jsonl(path: Path, rows) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def normalize_name(name: str) -> str:
    """Fold a place/authority name for matching: lowercase, umlauts->ascii,
    drop official suffixes after comma and bracketed additions."""
    s = name.lower().strip()
    s = s.split(",")[0]
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.split())


class Http:
    """requests.Session with per-instance politeness delay and retry."""

    def __init__(self, delay: float = 0.3, retries: int = 3):
        self.delay = delay
        self.retries = retries
        self.s = requests.Session()
        self.s.headers["User-Agent"] = (
            "Amtsgraph/1.0 (open-data pipeline; contact: see repo)")
        self._last = 0.0

    def get(self, url: str, **kw) -> requests.Response:
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                self._last = time.time()
                r = self.s.get(url, timeout=kw.pop("timeout", 30), **kw)
                if r.status_code == 429:        # back off hard on rate limit
                    time.sleep(10 * (attempt + 1))
                    raise requests.HTTPError("429")
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} on {url}")
                return r
            except Exception as exc:           # noqa: BLE001 — retry then re-raise
                last_exc = exc
                time.sleep(2 * (attempt + 1))
        raise last_exc  # type: ignore[misc]


class HttpPool:
    """Thread-local Http instances: N workers x per-worker delay.

    Effective rate ~= workers / delay req/s; each worker stays polite on
    its own connection, retries and 429-backoff included.
    """

    def __init__(self, delay: float = 0.25):
        import threading
        self.delay = delay
        self._local = threading.local()

    def get(self, url: str, **kw) -> requests.Response:
        if not hasattr(self._local, "http"):
            self._local.http = Http(delay=self.delay)
        return self._local.http.get(url, **kw)
