# KV-Bench

Measures one thing that Cloudflare Workers KV documents only as a ceiling:
**after you write a key, how long until a Worker can actually read it back?**
The docs say KV is "eventually consistent — up to 60 seconds." That 60s is a
ceiling, not a measurement. This harness measures the real distribution.

## What it measures

- **Mode A — fresh-key read-after-write.** Write a brand-new key, then poll a
  Worker's `get()` until the value appears. Because the key is new every trial,
  the first read is a cache miss that goes to the central store — so this is a
  clean read-after-write latency with no stale-cache confound.
- **Mode B — overwrite cache window.** Write a key, read it once so the edge
  caches it, overwrite it, then poll until the new value appears. This measures
  the edge-cache staleness window (KV `cacheTtl`, minimum **30s**, default 60s).
- **Mode C — cacheTtl sweep (`mr/cachettl.py`).** Same as B, but read with an
  explicit `cacheTtl` and sweep it (30/60/120/300/600s) across regions pinned by
  Durable Object `locationHint`. Shows the stale window scales *linearly* with the
  TTL you set — about 0.85× it — not just the 60s default Mode B measured.

The interesting question the modes separate: is KV's "up to 60s" about
*write propagation* (Mode A), *edge-cache TTL* (Mode B), and does that window move
with the `cacheTtl` you choose (Mode C — yes, in direct proportion)?

## How timing is kept honest

The driver times every trial from its own wall clock (`time.monotonic()`),
outside the edge. The Worker reports its own `Date.now()` too, but that value is
**not** trusted: the Workers runtime freezes its clock between I/O, so an
in-isolate timer cannot measure this. (Learned the hard way on a previous bench.)

## What it does NOT claim

- **Mode A/B run from one region.** The single-region driver's requests land in
  one edge location — read-after-write *at the writing region*, not cross-region
  propagation. The cross-region window and the cacheTtl sweep use Durable Objects
  pinned by `locationHint` to reach other regions.
- One account, free tier, one time window. Stated as unmeasured, never
  extrapolated. The sweep's ~0.85× (not 1.0×) shortfall below the nominal TTL is
  measured but its mechanism is not claimed. See `results/FINDINGS.md`.

## Run it

1. Create a KV namespace and put its id in `wrangler.toml` (use a throwaway copy
   — the committed file keeps a placeholder on purpose).
2. `wrangler deploy`
3. Set the gate secret: `wrangler secret put BENCH_TOKEN`
4. Point a driver at the deployed Worker and run it:
   - `BENCH_BASE` = the Worker origin (`https://kv-bench.<subdomain>.workers.dev`)
   - `BENCH_TOKEN` = the same secret
   - `python drive.py` — single-region Modes A + B.
   - Cross-region + sweep use the `mr/` Worker (Durable Objects pinned by
     `locationHint`): deploy `mr/`, then `python mr/drive.py` (cross-region window)
     or `python mr/cachettl.py` (Mode C — the cacheTtl sweep).

Defaults: 50 fresh-key trials + 8 cache-window trials (Modes A/B); 30 cells
(5 TTLs × 3 regions × 2 trials) for the sweep. KV free tier is 1,000 writes/day
and 100,000 reads/day, a separate bucket from D1 — every run here spends well
under 10% of either.

F-Keys LLC | www.f-keys.com
