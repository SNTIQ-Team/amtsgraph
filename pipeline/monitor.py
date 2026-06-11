#!/usr/bin/env python3
"""Realtime dashboard for the full-Germany harvest (btop-style, read-only).

Watches the snapshot files the running pipeline appends to, computes rates
over a sliding window and ETAs per phase, tails the logs and error files.
Never touches the harvest itself.

Stats history -> ./.tmp/monitor_stats.jsonl (one sample per refresh)
Error mirror  -> ./.tmp/errors.log

Usage:
    python3 monitor.py             # live dashboard, refresh every 2s
    python3 monitor.py --once      # print one frame and exit (no ANSI)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAP = HERE.parent / "data" / "snapshots"
TMP = HERE / ".tmp"
TMP.mkdir(exist_ok=True)

MATTERS = 14  # core matter set size used by run_full.sh


def snap_dir(source: str) -> Path:
    """Latest existing snapshot dir for a source — a harvest started before
    midnight keeps writing into yesterday's dir, so never derive the day
    from the calendar."""
    base = SNAP / source
    if base.is_dir():
        days = sorted(p for p in base.iterdir() if p.is_dir())
        if days:
            return days[-1]
    return base / "missing"

C = {"r": "\033[0m", "b": "\033[1m", "dim": "\033[2m",
     "red": "\033[31m", "grn": "\033[32m", "yel": "\033[33m",
     "cya": "\033[36m"}


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
    return n


def tail_lines(path: Path, n: int = 3, width: int = 110) -> list[str]:
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return [l[:width] for l in lines[-n:] if l.strip()]
    except OSError:
        return []


def distinct_plz_total() -> int:
    """Total PLZ to process (from the openplz snapshot), cached."""
    cache = TMP / "plz_total.txt"
    if cache.exists():
        return int(cache.read_text())
    loc = snap_dir("openplz") / "localities.jsonl"
    if not loc.exists():
        return 0
    plz = set()
    for line in open(loc, encoding="utf-8"):
        try:
            plz.add(json.loads(line)["postalCode"])
        except (json.JSONDecodeError, KeyError):
            pass
    cache.write_text(str(len(plz)))
    return len(plz)


def gemeinden_total() -> int:
    cache = TMP / "gem_total.txt"
    if cache.exists():
        return int(cache.read_text())
    f = snap_dir("openplz") / "municipalities.jsonl"
    n = count_lines(f)
    if n:
        cache.write_text(str(n))
    return n


def places_done_plz() -> int:
    """PLZ already processed in phase 1 = distinct plz in places + misses."""
    plz = set()
    for fname in ("places.jsonl", "places_misses.jsonl"):
        f = snap_dir("justiz") / fname
        if not f.exists():
            continue
        for line in open(f, encoding="utf-8"):
            try:
                plz.add(json.loads(line)["plz"])
            except (json.JSONDecodeError, KeyError):
                pass
    return len(plz)


def places_total() -> int:
    """Distinct (plz, ortk) in the register — the per-place harvest unit."""
    cache = TMP / "places_total.txt"
    if cache.exists():
        return int(cache.read_text())
    f = snap_dir("justiz") / "places.jsonl"
    if not f.exists():
        return 0
    seen = set()
    for line in open(f, encoding="utf-8"):
        try:
            r = json.loads(line)
            seen.add((r["plz"], r["ortk"]))
        except (json.JSONDecodeError, KeyError):
            pass
    cache.write_text(str(len(seen)))
    return len(seen)


def gs_keys_total() -> int:
    cache = TMP / "gs_total.txt"
    f = snap_dir("justiz") / "places.jsonl"
    if not f.exists():
        return 0
    keys = set()
    for line in open(f, encoding="utf-8"):
        try:
            keys.add(json.loads(line)["gs_key"])
        except (json.JSONDecodeError, KeyError):
            pass
    cache.write_text(str(len(keys)))
    return len(keys)


class Rate:
    """Sliding-window rate over the last `window` seconds."""

    def __init__(self, window: float = 120.0):
        self.window = window
        self.samples: deque[tuple[float, int]] = deque()

    def update(self, count: int) -> float:
        now = time.time()
        self.samples.append((now, count))
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        if len(self.samples) < 2:
            return 0.0
        (t0, c0), (t1, c1) = self.samples[0], self.samples[-1]
        return (c1 - c0) / (t1 - t0) if t1 > t0 else 0.0


def bar(done: int, total: int, width: int = 36) -> str:
    if total <= 0:
        return f"[{'?' * width}]"
    frac = min(1.0, done / total)
    fill = int(frac * width)
    return f"[{'█' * fill}{'░' * (width - fill)}] {frac * 100:5.1f}%"


def eta(done: int, total: int, rate: float) -> str:
    if total <= 0 or rate <= 0:
        return "—"
    rem = max(0, total - done) / rate
    if rem < 90:
        return f"{rem:.0f}s"
    if rem < 5400:
        return f"{rem / 60:.0f}m"
    return f"{rem / 3600:.1f}h"


def frame(rates: dict[str, Rate], color: bool) -> tuple[str, dict]:
    def c(code, s):
        return f"{C[code]}{s}{C['r']}" if color else str(s)

    jd = snap_dir("justiz")
    pd = snap_dir("pvog")

    plz_total = distinct_plz_total()
    plz_done = places_done_plz()
    places_active = plz_done < plz_total

    gs_total = 0 if places_active else gs_keys_total()
    chains_total = gs_total * MATTERS
    chains_done = sum(count_lines(f) for f in jd.glob("chains*.jsonl"))
    chain_errs = sum(count_lines(f) for f in jd.glob("chain_errors*.jsonl"))

    gem_total = gemeinden_total()
    gem_done = sum(count_lines(f) for f in pd.glob("done*.jsonl"))
    pvog_comp = sum(count_lines(f) for f in pd.glob("competences*.jsonl"))
    pvog_miss = sum(count_lines(f) for f in pd.glob("misses*.jsonl"))

    r_places = rates["places"].update(plz_done)
    r_chains = rates["chains"].update(chains_done)
    r_pvog = rates["pvog"].update(gem_done)

    now = datetime.now().strftime("%H:%M:%S")
    L = []
    L.append(c("b", f"  Amtsgraph — harvest monitor   {now}"))
    L.append("")
    st = c("grn", "done") if not places_active else c("yel", f"{r_places:5.1f} PLZ/s")
    L.append(f"  {c('cya', 'JUSTIZ places ')} {bar(plz_done, plz_total)}  "
             f"{plz_done}/{plz_total} PLZ   {st}   ETA {eta(plz_done, plz_total, r_places)}")

    pch_files = list(jd.glob("pchains*.jsonl"))
    if pch_files:
        # per-place backfill: every (plz, ortk) x matter fetched directly
        pch_done = sum(count_lines(f) for f in pch_files)
        pch_total = places_total() * MATTERS
        r_pch = rates.setdefault("pchains", Rate()).update(pch_done)
        st = (c("grn", "done") if pch_total and pch_done >= pch_total
              else c("yel", f"{r_pch:5.1f} req/s"))
        L.append(f"  {c('cya', 'JUSTIZ pchains')} {bar(pch_done, pch_total)}  "
                 f"{pch_done}/{pch_total} (place×matter)   {st}   "
                 f"ETA {eta(pch_done, pch_total, r_pch)}   "
                 + (c("red", f"errors: {chain_errs}") if chain_errs else "errors: 0"))
        chains_done = pch_done
        chains_total = pch_total
    elif places_active:
        L.append(f"  {c('cya', 'JUSTIZ chains ')} {c('dim', 'waiting for places phase…')}")
    else:
        st = (c("grn", "done") if chains_done >= chains_total and chains_total
              else c("yel", f"{r_chains:5.1f} chains/s"))
        L.append(f"  {c('cya', 'JUSTIZ chains ')} {bar(chains_done, chains_total)}  "
                 f"{chains_done}/{chains_total} (gs={gs_total}×{MATTERS})   {st}   "
                 f"ETA {eta(chains_done, chains_total, r_chains)}   "
                 + (c("red", f"errors: {chain_errs}") if chain_errs else "errors: 0"))

    st = (c("grn", "done") if gem_total and gem_done >= gem_total
          else c("yel", f"{r_pvog:5.2f} Gem/s"))
    L.append(f"  {c('cya', 'PVOG Behörden ')} {bar(gem_done, gem_total)}  "
             f"{gem_done}/{gem_total} Gemeinden   {st}   "
             f"ETA {eta(gem_done, gem_total, r_pvog)}   "
             f"competences: {pvog_comp}  misses: {pvog_miss}")
    L.append("")

    err_lines = []
    for name, f in (("chains", jd / "chain_errors.jsonl"),
                    ("justiz.log", HERE / "justiz_chains.log"),
                    ("pvog.log", HERE / "pvog.log")):
        for line in tail_lines(f, 2):
            low = line.lower()
            if any(w in low for w in ("error", "traceback", "warn", "fatal",
                                      "exception", "mismatch")):
                err_lines.append(f"  {c('red', name + ':')} {line}")
    if err_lines:
        L.append(c("b", "  recent errors/warnings"))
        L += err_lines[-6:]
        L.append("")

    L.append(c("b", "  log tails"))
    for name, f in (("justiz", HERE / "justiz_chains.log"),
                    ("pvog", HERE / "pvog.log")):
        for line in tail_lines(f, 2):
            L.append(f"  {c('dim', name + ' | ' + line)}")

    stats = {"ts": now, "plz_done": plz_done, "plz_total": plz_total,
             "chains_done": chains_done, "chains_total": chains_total,
             "chain_errors": chain_errs, "gemeinden_done": gem_done,
             "gemeinden_total": gem_total, "pvog_competences": pvog_comp,
             "pvog_misses": pvog_miss,
             "rate_places": round(r_places, 2),
             "rate_chains": round(r_chains, 2),
             "rate_pvog": round(r_pvog, 3)}
    return "\n".join(L), stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    rates = {"places": Rate(), "chains": Rate(), "pvog": Rate()}
    stats_f = open(TMP / "monitor_stats.jsonl", "a", encoding="utf-8")
    err_mirror = open(TMP / "errors.log", "a", encoding="utf-8")
    seen_errs = 0

    try:
        while True:
            text, stats = frame(rates, color=not args.once)
            stats_f.write(json.dumps(stats) + "\n")
            stats_f.flush()
            # mirror new chain errors into .tmp/errors.log
            ef = snap_dir("justiz") / "chain_errors.jsonl"
            cur = count_lines(ef)
            if cur > seen_errs and ef.exists():
                lines = open(ef, encoding="utf-8").read().splitlines()
                for l in lines[seen_errs:cur]:
                    err_mirror.write(f"{stats['ts']} {l}\n")
                err_mirror.flush()
                seen_errs = cur
            if args.once:
                print(text)
                return 0
            sys.stdout.write("\033[2J\033[H" + text + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        stats_f.close()
        err_mirror.close()


if __name__ == "__main__":
    sys.exit(main())
