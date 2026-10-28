# ============================================================
# KV-BENCH-MR CACHETTL SWEEP v0.3.0
# Shows the Workers KV cross-region stale window SCALES with the cacheTtl set on
# get(). The original single-TTL run measured ~53 s at the 60 s default; the docs
# say an overwrite "will not be visible until the cached value expires" but give no
# curve. This sweeps cacheTtl across its documented range and measures the window.
# F-Keys LLC | www.f-keys.com
# ============================================================
# PER-CELL PROTOCOL  (one cell = one region x one ttl x one trial, own key)
#   1. write(key, "A")                              -- central
#   2. wait WARM_WAIT for central visibility, then ctread(key, region, ttl)
#      -- the region's colo COLD-misses, fetches "A", and caches it for ttl s.
#      Record t_warm (driver wall clock). Retry on a fresh key if it cached a null.
#   3. write(key, "B")                              -- overwrite; record t_write
#   4. poll ctread(key, region, ttl) every POLL s until it returns "B"
#      -- window_from_warm = t_seen - t_warm  (~= ttl: the cache's own lifetime)
#         window_from_write = t_seen - t_write (the stale window a writer sees)
#
# Cells run CONCURRENTLY, each on its own key and its own thread, so one cell's
# poll schedule never delays another's first observation (the interleaving trap
# the single-region driver documents). The only timing limit is the poll interval,
# which is seconds against windows of tens-to-hundreds of seconds.
#
# Regions are pinned via Durable Object locationHint; each reports the colo it ran
# in, so placement is verified. TIMING AUTHORITY: time.monotonic() in THIS process.
# STDLIB ONLY. Browser User-Agent (workers.dev rejects Python-urllib).
#
# CONFIG (env): BENCH_BASE, BENCH_TOKEN,
#   BENCH_REGIONS (default wnam,weur,apac),
#   BENCH_TTLS (default 30,60,120,300,600),
#   BENCH_TRIALS (default 2), BENCH_POLL (s, default 2.0),
#   BENCH_WARM_WAIT (s, default 3.0), BENCH_SLACK (s, default 120),
#   BENCH_OUT (default ct_sweep.json).
# ============================================================

import json
import os
import sys
import time
import uuid
import threading
import statistics
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.environ.get("BENCH_BASE", "").rstrip("/")
TOKEN = os.environ.get("BENCH_TOKEN", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REGIONS = [r.strip() for r in os.environ.get("BENCH_REGIONS", "wnam,weur,apac").split(",") if r.strip()]
TTLS = [int(t) for t in os.environ.get("BENCH_TTLS", "30,60,120,300,600").split(",") if t.strip()]
TRIALS = int(os.environ.get("BENCH_TRIALS", "2"))
POLL = float(os.environ.get("BENCH_POLL", "2.0"))
WARM_WAIT = float(os.environ.get("BENCH_WARM_WAIT", "3.0"))
SLACK = float(os.environ.get("BENCH_SLACK", "120"))
OUT = os.environ.get("BENCH_OUT", "ct_sweep.json")

PRINT_LOCK = threading.Lock()


def say(msg):
    with PRINT_LOCK:
        print(msg)
        sys.stdout.flush()


def scrub_base(base):
    # Replace the account subdomain so published data carries no account id:
    # https://kv-bench-mr.<sub>.workers.dev -> https://kv-bench-mr.<subdomain>.workers.dev
    try:
        host = base.split("//")[-1]
        parts = host.split(".")
        if len(parts) >= 4 and parts[-2:] == ["workers", "dev"]:
            parts[1] = "<subdomain>"
            return "https://" + ".".join(parts)
    except Exception as err:
        print("scrub_base:", err)
    return "https://kv-bench-mr.<subdomain>.workers.dev"


def call(path, timeout=20):
    # One HTTP call to the Worker. Thread-safe: no shared mutable state.
    url = BASE + path
    method = "POST" if path.startswith("/mr/write") else "GET"
    req = urllib.request.Request(
        url, method=method,
        headers={"User-Agent": UA, "X-Bench-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as err:
        # a transient network error on one poll must not kill the cell
        return {"error": str(err)}


def write(key, val):
    return call("/mr/write?key=%s&val=%s" % (urllib.parse.quote(key), urllib.parse.quote(val)))


def ctread(key, region, ttl):
    return call("/mr/ctread?key=%s&region=%s&ttl=%d"
                % (urllib.parse.quote(key), urllib.parse.quote(region), ttl))


def run_cell(region, ttl, trial, run_id, index):
    # Stagger starts slightly so concurrent cells do not all hit at once.
    time.sleep(index * 0.1)
    base_key = "ct:%s:%s:%d:t%d" % (run_id, region, ttl, trial)

    # --- warm: cache "A" at the region's colo, retry on a fresh key if we cached a null
    key = base_key
    colo = None
    t_warm = None
    for attempt in range(3):
        key = base_key if attempt == 0 else base_key + "r%d" % attempt
        write(key, "A")
        time.sleep(WARM_WAIT)
        r = ctread(key, region, ttl)
        if r.get("val") == "A":
            t_warm = time.monotonic()
            colo = r.get("colo")
            break
    if t_warm is None:
        say("  %-5s ttl=%-3d t%d: WARM FAILED (never cached A)" % (region, ttl, trial))
        return {"region": region, "ttl": ttl, "trial": trial, "key": key,
                "colo": colo, "status": "warm_failed"}

    # --- overwrite centrally, then watch this region keep serving the stale "A"
    write(key, "B")
    t_write = time.monotonic()

    stale_polls = 0
    t_seen = None
    deadline = t_warm + ttl + SLACK
    while time.monotonic() < deadline:
        r = ctread(key, region, ttl)
        v = r.get("val")
        if v == "B":
            t_seen = time.monotonic()
            break
        if v == "A":
            stale_polls += 1
        time.sleep(POLL)

    if t_seen is None:
        say("  %-5s ttl=%-3d t%d [%s]: TIMEOUT (>%ds, stale_polls=%d)"
            % (region, ttl, trial, colo, int(ttl + SLACK), stale_polls))
        return {"region": region, "ttl": ttl, "trial": trial, "key": key,
                "colo": colo, "status": "timeout", "stale_polls": stale_polls}

    w_warm = t_seen - t_warm
    w_write = t_seen - t_write
    gap = t_write - t_warm
    say("  %-5s ttl=%-3d t%d [%s]: window_from_warm=%.1fs (ratio %.2f) "
        "window_from_write=%.1fs stale_polls=%d"
        % (region, ttl, trial, colo, w_warm, w_warm / ttl, w_write, stale_polls))
    return {"region": region, "ttl": ttl, "trial": trial, "key": key, "colo": colo,
            "status": "ok", "window_from_warm_s": round(w_warm, 2),
            "window_from_write_s": round(w_write, 2), "warm_to_write_gap_s": round(gap, 2),
            "ratio_to_ttl": round(w_warm / ttl, 3), "stale_polls": stale_polls}


def run():
    if not BASE or not TOKEN:
        sys.exit("Set BENCH_BASE and BENCH_TOKEN.")
    run_id = uuid.uuid4().hex[:8]
    cells = []
    idx = 0
    for ttl in TTLS:
        for region in REGIONS:
            for trial in range(TRIALS):
                cells.append((region, ttl, trial, idx))
                idx += 1

    say("cacheTtl sweep: run_id=%s regions=%s ttls=%s trials=%d cells=%d"
        % (run_id, ",".join(REGIONS), ",".join(str(t) for t in TTLS), TRIALS, len(cells)))
    say("writes budget = %d puts (2/cell); reads are cache hits until expiry, a few "
        "hundred total. Longest cell ~= max ttl + poll." % (2 * len(cells)))
    say("running all cells concurrently...")

    results = []
    with ThreadPoolExecutor(max_workers=len(cells)) as pool:
        futs = [pool.submit(run_cell, region, ttl, trial, run_id, index)
                for (region, ttl, trial, index) in cells]
        for f in as_completed(futs):
            results.append(f.result())

    # --- summary grouped by ttl: does the window track the ttl?
    say("")
    say("=== stale window vs cacheTtl (window_from_warm, seconds) ===")
    by_ttl = {}
    for r in results:
        if r.get("status") == "ok":
            by_ttl.setdefault(r["ttl"], []).append(r["window_from_warm_s"])
    say("  %-6s %-4s %-8s %-8s %-8s %-6s" % ("ttl", "n", "min", "median", "max", "med/ttl"))
    for ttl in TTLS:
        vals = sorted(by_ttl.get(ttl, []))
        if not vals:
            say("  %-6d %-4d (no ok cells)" % (ttl, 0))
            continue
        med = statistics.median(vals)
        say("  %-6d %-4d %-8.1f %-8.1f %-8.1f %-6.2f"
            % (ttl, len(vals), vals[0], med, vals[-1], med / ttl))

    ok = sum(1 for r in results if r.get("status") == "ok")
    out = {
        "run_id": run_id, "base": scrub_base(BASE),
        "regions": REGIONS, "ttls": TTLS, "trials": TRIALS,
        "poll_s": POLL, "resolution_note": "window measured to the first poll after expiry; resolution = poll interval",
        "cells": len(cells), "ok": ok, "results": sorted(
            results, key=lambda r: (r["ttl"], r["region"], r["trial"])),
    }
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=2)
    say("")
    say("ok cells: %d/%d. writes=%d. Wrote %s" % (ok, len(cells), 2 * len(cells), OUT))


if __name__ == "__main__":
    run()
