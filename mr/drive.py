# ============================================================
# KV-BENCH-MR DRIVER v0.2.0
# Measures the CROSS-REGION staleness window of Cloudflare Workers KV: after a
# write, how long does a reader in a different region keep serving the stale
# value it had cached? This is the real number behind the docs' "up to 60s".
# F-Keys LLC | www.f-keys.com
# ============================================================
# PER-TRIAL PROTOCOL
#   1. write(key, OLD)                    -- from the coordinator colo
#   2. prime every region until it reads OLD   -- each region now caches OLD at its colo
#   3. write(key, NEW)                    -- overwrite; record t0 (driver wall clock)
#   4. poll check(region) for every region until it returns NEW
#      -> per-region flip latency = (first time NEW seen) - t0
#
# Regions are pinned via Durable Object locationHint in the Worker; each region
# reports the colo it actually ran in, so placement is verified, not assumed.
#
# TIMING AUTHORITY: time.monotonic() in THIS process, outside the edge.
# STDLIB ONLY. Browser User-Agent (workers.dev rejects Python-urllib).
#
# CONFIG (env): BENCH_BASE, BENCH_TOKEN,
#   BENCH_REGIONS (default enam,wnam,weur,eeur,apac,oc),
#   BENCH_N (trials, default 6), BENCH_POLL (s, default 1.0),
#   BENCH_PRIME_TIMEOUT (default 20), BENCH_FLIP_TIMEOUT (default 90),
#   BENCH_OUT (default mr_sweep.json).
# ============================================================

import json
import os
import sys
import time
import uuid
import statistics
import urllib.request
import urllib.parse
import urllib.error

BASE = os.environ.get("BENCH_BASE", "").rstrip("/")
TOKEN = os.environ.get("BENCH_TOKEN", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REGIONS = [r.strip() for r in os.environ.get("BENCH_REGIONS", "enam,wnam,weur,eeur,apac,oc").split(",") if r.strip()]
N = int(os.environ.get("BENCH_N", "6"))
POLL = float(os.environ.get("BENCH_POLL", "1.0"))
PRIME_TIMEOUT = float(os.environ.get("BENCH_PRIME_TIMEOUT", "20"))
FLIP_TIMEOUT = float(os.environ.get("BENCH_FLIP_TIMEOUT", "90"))
OUT = os.environ.get("BENCH_OUT", "mr_sweep.json")


def call(path):
    url = BASE + path
    method = "POST" if path.startswith("/mr/write") else "GET"
    req = urllib.request.Request(
        url, method=method,
        headers={"User-Agent": UA, "X-Bench-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def write(key, val):
    return call("/mr/write?key=%s&val=%s" % (urllib.parse.quote(key), urllib.parse.quote(val)))


def probe(kind, key, region):
    # kind is "prime" or "check". Returns {region, colo, val, serverTime}.
    return call("/mr/%s?key=%s&region=%s" % (kind, urllib.parse.quote(key), urllib.parse.quote(region)))


def prime_region(key, region, want):
    # Poll /mr/prime until the region reads `want` (so it has cached it). Return colo or None.
    start = time.monotonic()
    while True:
        r = probe("prime", key, region)
        if r.get("val") == want:
            return r.get("colo")
        if time.monotonic() - start >= PRIME_TIMEOUT:
            return None
        time.sleep(0.3)


def placement():
    # Verify locationHint: prime every region on one key and print the colo each landed in.
    if not BASE or not TOKEN:
        sys.exit("Set BENCH_BASE and BENCH_TOKEN.")
    key = "mr:placement:" + uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    write(key, nonce)
    seen = {}
    for region in REGIONS:
        colo = prime_region(key, region, nonce)
        seen[region] = colo
        print("  %-5s -> colo %s" % (region, colo))
    distinct = sorted(set(c for c in seen.values() if c))
    print("distinct colos: %d of %d regions (%s)" % (len(distinct), len(REGIONS), ",".join(distinct)))
    return seen


def run():
    if not BASE or not TOKEN:
        sys.exit("Set BENCH_BASE and BENCH_TOKEN.")
    print("placement check:")
    placement_map = placement()
    run_id = uuid.uuid4().hex[:8]
    results = {"run_id": run_id, "base": BASE, "regions": REGIONS,
               "placement": placement_map, "trials": []}
    # per-region accumulators
    per_region = dict((r, []) for r in REGIONS)
    colo_seen = dict((r, set()) for r in REGIONS)

    for i in range(N):
        key = "mr:%s:%d" % (run_id, i)
        oldv = uuid.uuid4().hex
        newv = uuid.uuid4().hex
        write(key, oldv)
        primed = {}
        for region in REGIONS:
            primed[region] = prime_region(key, region, oldv)
        write(key, newv)
        t0 = time.monotonic()

        pending = set(REGIONS)
        flips = dict((r, None) for r in REGIONS)
        while pending:
            now = time.monotonic()
            if now - t0 >= FLIP_TIMEOUT:
                break
            for region in list(pending):
                r = probe("check", key, region)
                if r.get("colo"):
                    colo_seen[region].add(r.get("colo"))
                if r.get("val") == newv:
                    flips[region] = time.monotonic() - t0
                    pending.discard(region)
            if pending:
                time.sleep(POLL)

        trial = {"key": key, "primed_colo": primed, "flip_s": dict(flips)}
        results["trials"].append(trial)
        for region in REGIONS:
            per_region[region].append(flips[region])
        line = " ".join("%s=%s" % (r, ("%.1f" % flips[r]) if flips[r] is not None else "TO") for r in REGIONS)
        print("trial %d/%d: %s" % (i + 1, N, line))

    print("")
    print("=== cross-region flip latency (seconds) ===")
    for region in REGIONS:
        vals = [x for x in per_region[region] if x is not None]
        miss = len(per_region[region]) - len(vals)
        colos = ",".join(sorted(colo_seen[region])) or "?"
        if not vals:
            print("  %-5s [%s]: 0 resolved, %d timed out (>= %ds)" % (region, colos, miss, int(FLIP_TIMEOUT)))
            continue
        vs = sorted(vals)
        p90 = vs[min(len(vs) - 1, int(round(0.9 * (len(vs) - 1))))]
        print("  %-5s [%s]: n=%d miss=%d | min=%.1f med=%.1f p90=%.1f max=%.1f"
              % (region, colos, len(vs), miss, vs[0], statistics.median(vals), p90, vs[-1]))

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(results, f, indent=2)
    writes = N * 2 + 1
    print("")
    print("budget: writes=%d, regions=%d, trials=%d. Wrote %s" % (writes, len(REGIONS), N, OUT))


if __name__ == "__main__":
    run()
