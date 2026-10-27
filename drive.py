# ============================================================
# KV-BENCH DRIVER v0.1.0
# Times Cloudflare Workers KV read-after-write visibility from outside the edge.
# F-Keys LLC | www.f-keys.com
# ============================================================
# WHAT IT MEASURES
#   Mode A (fresh-key read-after-write): write a brand-new unique key, then poll
#     /read until the value appears. Measures how fast a just-written key becomes
#     readable from this region. The key is new every trial, so there is no
#     stale-cache confound -- the first read is a cache miss that goes to the
#     central store.
#   Mode B (overwrite cache window): write a key, read it once so the edge caches
#     it, overwrite it with a new value, then poll until the new value appears.
#     Measures the edge-cache staleness window (KV cacheTtl, minimum 60s).
#
# TIMING AUTHORITY: time.monotonic() in THIS process, outside the edge. The
#   Worker also reports Date.now() but it is NOT trusted -- the Workers runtime
#   freezes its clock between I/O, so an in-isolate timer cannot measure this.
#
# CONFIG (environment):
#   BENCH_BASE    Worker origin, e.g. https://kv-bench.<subdomain>.workers.dev
#   BENCH_TOKEN   gate secret (must match the Worker's BENCH_TOKEN)
#   BENCH_NA      fresh-key trials      (default 50)
#   BENCH_NB      cache-window trials   (default 8)
#   BENCH_POLL    seconds between polls (default 0.2)
#   BENCH_TIMEOUT_A / BENCH_TIMEOUT_B   per-trial poll ceilings (20s / 90s)
#   BENCH_OUT     raw-results path      (default sweep.json)
#
# STDLIB ONLY. Sends a browser User-Agent because workers.dev rejects the
# default Python-urllib agent via a Cloudflare managed rule.
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

N_A = int(os.environ.get("BENCH_NA", "50"))
N_B = int(os.environ.get("BENCH_NB", "8"))
POLL_S = float(os.environ.get("BENCH_POLL", "0.2"))
TIMEOUT_A = float(os.environ.get("BENCH_TIMEOUT_A", "20"))
TIMEOUT_B = float(os.environ.get("BENCH_TIMEOUT_B", "90"))
OUT = os.environ.get("BENCH_OUT", "sweep.json")


def call(path):
    # One request. Returns parsed JSON. Raises urllib errors to the caller.
    url = BASE + path
    method = "POST" if path.startswith("/write") else "GET"
    req = urllib.request.Request(
        url, method=method,
        headers={"User-Agent": UA, "X-Bench-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def write_key(k, v):
    return call("/write?key=%s&val=%s" % (urllib.parse.quote(k), urllib.parse.quote(v)))


def read_key(k):
    return call("/read?key=%s" % urllib.parse.quote(k))


def poll_until(k, want, timeout):
    # Poll /read until val == want, or timeout. Returns (latency_s_or_None, polls).
    start = time.monotonic()
    polls = 0
    while True:
        polls += 1
        got = read_key(k)
        if got.get("val") == want:
            return (time.monotonic() - start, polls)
        if time.monotonic() - start >= timeout:
            return (None, polls)
        time.sleep(POLL_S)


def preflight():
    if not BASE or not TOKEN:
        sys.exit("Set BENCH_BASE and BENCH_TOKEN in the environment.")
    # confirm the gate + binding answer before burning the budget
    k = "bench:preflight:" + uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    write_key(k, nonce)
    lat, polls = poll_until(k, nonce, 10)
    if lat is None:
        sys.exit("Preflight failed: wrote a key but never read it back within 10s.")
    print("preflight ok: fresh key readable in %.3fs (%d polls)" % (lat, polls))


def summary(label, lats):
    done = [x for x in lats if x is not None]
    miss = len(lats) - len(done)
    if not done:
        print("%s: 0/%d resolved (all timed out)" % (label, len(lats)))
        return
    done_sorted = sorted(done)
    p90 = done_sorted[min(len(done_sorted) - 1, int(round(0.9 * (len(done_sorted) - 1))))]
    print("%s: n=%d resolved, %d timed out | min=%.3fs median=%.3fs p90=%.3fs max=%.3fs"
          % (label, len(done), miss, done_sorted[0], statistics.median(done), p90, done_sorted[-1]))


def run():
    preflight()
    run_id = uuid.uuid4().hex[:8]
    results = {"run_id": run_id, "base": BASE, "mode_a": [], "mode_b": []}

    # ---- Mode A: fresh-key read-after-write ----
    a_lats = []
    for i in range(N_A):
        k = "bench:a:%s:%d" % (run_id, i)
        nonce = uuid.uuid4().hex
        write_key(k, nonce)
        lat, polls = poll_until(k, nonce, TIMEOUT_A)
        a_lats.append(lat)
        results["mode_a"].append({"key": k, "latency_s": lat, "polls": polls})
        sys.stdout.write("A %d/%d %s\r" % (i + 1, N_A, ("%.3fs" % lat) if lat is not None else "TIMEOUT"))
        sys.stdout.flush()
    print("")
    summary("MODE A (fresh-key read-after-write)", a_lats)

    # ---- Mode B: overwrite cache window ----
    b_lats = []
    for i in range(N_B):
        k = "bench:b:%s:%d" % (run_id, i)
        first = uuid.uuid4().hex
        write_key(k, first)
        poll_until(k, first, TIMEOUT_A)   # make the key live + cached at this edge
        read_key(k)                       # ensure it is cached
        second = uuid.uuid4().hex
        write_key(k, second)              # overwrite; cached readers should lag
        lat, polls = poll_until(k, second, TIMEOUT_B)
        b_lats.append(lat)
        results["mode_b"].append({"key": k, "latency_s": lat, "polls": polls})
        sys.stdout.write("B %d/%d %s\r" % (i + 1, N_B, ("%.3fs" % lat) if lat is not None else "TIMEOUT"))
        sys.stdout.flush()
    print("")
    summary("MODE B (overwrite cache window)", b_lats)

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(results, f, indent=2)
    # rough budget accounting
    a_writes = N_A
    b_writes = N_B * 2
    a_reads = sum(r["polls"] for r in results["mode_a"])
    b_reads = sum(r["polls"] for r in results["mode_b"]) + N_B  # + the cache-priming reads
    print("budget: writes=%d (A %d + B %d), reads=%d. Wrote %s"
          % (a_writes + b_writes, a_writes, b_writes, a_reads + b_reads, OUT))


if __name__ == "__main__":
    run()
