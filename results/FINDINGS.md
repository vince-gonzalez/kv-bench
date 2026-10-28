# KV-Bench Findings v0.3.0

Cloudflare Workers KV, Workers **free** tier, 2026-10-27 / 2026-10-28.
Raw data: `sweep.json` (single-region, run `580899c3`),
`mr_sweep.json` (cross-region, run `bdde13aa`),
`ct_sweep.json` (cacheTtl sweep, run `017c364a`).

CHANGE LOG v0.2.0 → v0.3.0
- Swept `cacheTtl` across its documented range (30/60/120/300/600 s) in 3 regions,
  2 trials each, to answer the question v0.2.0 left open: does the stale window
  move with the `cacheTtl` you set? It does — linearly (`mr/cachettl.py`).
- Corrected a spec detail: the `cacheTtl` floor is **30 s**, not 60 s; 60 s is the
  default (Cloudflare KV read docs).

CHANGE LOG v0.1.0 → v0.2.0
- Added the cross-region half: 6 Durable Objects pinned by `locationHint` to 6
  distinct colos (ATL, SJC, MAD, VIE, SIN, MEL — 5 continents, verified by cdn
  trace, not assumed), each primed with the old value, then timed on how long it
  served stale after an overwrite.
- Headline rewritten: the two halves together explain what "up to 60 seconds" is.

## Question

Workers KV is documented as "eventually consistent — changes may take **up to 60
seconds** to propagate." Is that a delay you pay to read your own write, or a
cross-region cache effect — and if cross-region, is it really ~60s, or anywhere
in 0–60?

## The complete finding

**1. In the region that issued the write, read-after-write is sub-second.**
50/50 fresh keys readable on the first poll — median **0.149 s** (80–202 ms).
8/8 overwrites visible on the first poll — median **0.122 s**. You never wait to
read back what you just wrote, locally.

**2. In a region that had the old value cached, the stale read lasts ~one full
cacheTtl (~53 s), tightly clustered just under the 60 s ceiling — not spread
across 0–60.** 36 cross-region overwrites, 6 colos:

| region | colo | n | median (s) | p90 (s) | max (s) |
|---|---|---|---|---|---|
| enam | ATL | 6 | 51.7 | 52.4 | 53.0 |
| wnam | SJC | 6 | 52.7 | 53.4 | 53.9 |
| weur | MAD | 6 | 52.9 | 53.6 | 53.6 |
| eeur | VIE | 6 | 53.3 | 54.5 | 54.7 |
| apac | SIN | 6 | 53.5 | 54.4 | 54.9 |
| oc   | MEL | 6 | 52.8 | 53.4 | 53.7 |

Pooled: n=36, median **52.9 s**, p90 54.4 s, max **54.9 s** — never reaches 60.

**So "up to 60 seconds" is a ceiling, and in practice a cached cross-region
reader lands right near it (~53 s), because `put` does not push-invalidate
remote colo caches — those caches expire on their own TTL.** The 60 s is passive
cache expiry, not propagation latency.

## Why ~53 and not 60 (and why the per-region spread is not real)

Each region is primed (caches the old value, starting its 60 s TTL), then the
overwrite happens a few seconds later, and the flip is timed from the overwrite.
So the observed window = 60 s TTL − however long ago that region last read the
key. The six regions are primed in sequence, so the earliest-primed (enam) flips
soonest (51.7 s) and later-primed regions flip a bit later — the small per-region
differences track **prime order, not geography**. The underlying window is one
cacheTtl everywhere.

The single enam outlier (0.7 s on trial 6, vs ~52 s otherwise) is the writing
colo bleeding through: when the reader colo coincides with the colo that issued
the write, it is read-your-writes (finding 1), not a cached cross-region read.

## 3. The stale window scales with cacheTtl — linearly (v0.3.0)

v0.2.0 measured one point (the 60 s default) and left the obvious question open:
is that ~53 s a property of KV, or just of the default `cacheTtl`? It is the
`cacheTtl`. Setting `cacheTtl` explicitly on the read and sweeping it across the
documented range moves the window in direct proportion. 3 regions (SJC, MAD, SIN),
2 trials each, 30/30 cells resolved:

| cacheTtl (s) | n | min (s) | median (s) | max (s) | median ÷ cacheTtl |
|---|---|---|---|---|---|
| 30  | 6 | 20.4  | 24.4  | 25.4  | 0.81 |
| 60  | 6 | 51.3  | 54.5  | 55.0  | 0.91 |
| 120 | 6 | 102.0 | 104.3 | 105.9 | 0.87 |
| 300 | 6 | 255.2 | 257.4 | 259.3 | 0.86 |
| 600 | 6 | 509.4 | 513.0 | 514.5 | 0.85 |

The window measured is from the read that cached the value (the cache's own
lifetime). Over a 20× span the realized window stays a near-constant fraction of
the value set — about **0.85× the `cacheTtl`**. You get most of, but not quite the
full, nominal TTL, consistently. The 60 s row (~54.5 s) reproduces v0.2.0's ~53 s,
so the two runs agree at their shared point.

**The ~0.85, not ~1.0, is the honest surprise, and we do not over-explain it.** A
region that warms its own cache and is then overwritten should, in the simplest
model, serve stale for the full `cacheTtl`; the realized window is ~15 % shorter,
stably, at every TTL. Candidate causes are early cache eviction (the docs say the
value lives "until the end of the `cacheTtl` **or eviction**") and internal TTL
accounting we cannot see from outside. We measured the number; we do not claim the
mechanism for the shortfall.

**The write-path colo runs a few seconds shorter than the rest.** SJC (`wnam`) is
the colo the driver's `put` lands in, and across every TTL it flips ~3–5 s sooner
than MAD/SIN (ratio 0.68 at 30 s, where a fixed few-second offset is a big fraction,
rising toward the others as the TTL grows). The `put` write-through seeds SJC's
cache a few seconds before the warm read does, so SJC's entry expires first — the
same read-your-writes bleed-through finding 2 saw as its enam outlier, here as a
small steady offset.

## Method — why the timing is trustworthy

- **Placement verified, not assumed:** each Durable Object reported its real colo
  via cdn trace; all 6 `locationHint` regions landed in 6 distinct colos.
- **Timed from the driver's wall clock** (`time.monotonic()`), outside the edge.
  The Worker's `Date.now()` is reported but not trusted — the Workers runtime
  freezes its clock between I/O.
- Worker responses `cache-control: no-store`, so only KV/DO timing is measured.
- Fresh unique key per trial; in the sweep each cell has its own key and its own
  thread, so one cell's poll schedule never delays another's first observation.
- Budget across all three runs: ~139 writes + a few thousand reads/DO-requests —
  under 10% of the free-tier daily KV allowance, and a separate bucket from D1.

## What this still does NOT claim

- One account, one time window. The `cacheTtl` is now swept (finding 3), so the
  default-only caveat is retired; the ~15% shortfall below the nominal TTL is
  measured but its mechanism is not claimed.
- The sweep window is read to the first poll after expiry, so its resolution is the
  poll interval (2 s) — negligible against 30–600 s windows, but it is why the
  smallest TTL has the coarsest ratio.
- "No push-invalidation" is inferred from the passive-expiry result, not from
  Cloudflare internals.
- 6 colos × 6 trials (cross-region) and 3 regions × 2 trials per TTL (sweep) —
  enough to show the clustering and the linear scaling, not a global census.

## Relation to the Cloudflare docs

The KV docs already describe this behaviour qualitatively and correctly: changes
are "usually immediately visible ... at which they are made"; "may take up to 60
seconds or more to be visible in other global network locations"; take "longer in
locations which have recently read a previous version of a given key"; and "your
data is not sent automatically to every location's cache" — other locations update
"as their cached versions of the data time out." The default `cacheTtl` is 60 s.

The read docs also already tie the window to `cacheTtl` qualitatively: an
overwrite "will not be visible until the cached value expires," and a cached value
lives "until the end of the `cacheTtl` or eviction."

So this is **not** a documentation correction and there is no clarification PR to
file. The contribution is the measured numbers behind the docs' qualitative "up to
60 seconds or more": a cached cross-region reader lags by one `cacheTtl` (~53 s at
the default, clustered just under 60 s and never past it across 36 trials), the
writing region is sub-second, and the window scales linearly with the `cacheTtl`
you set — about 0.85× it from 30 s to 600 s. The docs say "up to the `cacheTtl`";
this says how much of it you actually get, where, and why.

## For a developer, in one line

Your own Worker reads your write immediately; a cache in another region can serve
the old value for most of your `cacheTtl` (measured ~0.85× it, 30 s to 600 s); a
`put` will not speed that up — only a shorter `cacheTtl` will, in direct proportion.

F-Keys LLC | www.f-keys.com
