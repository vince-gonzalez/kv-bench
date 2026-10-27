# KV-Bench Findings v0.2.0

Cloudflare Workers KV, Workers **free** tier, 2026-10-27.
Raw data: `sweep.json` (single-region, run `580899c3`),
`mr/mr_sweep.json` (cross-region, run `bdde13aa`).

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

## Method — why the timing is trustworthy

- **Placement verified, not assumed:** each Durable Object reported its real colo
  via cdn trace; all 6 `locationHint` regions landed in 6 distinct colos.
- **Timed from the driver's wall clock** (`time.monotonic()`), outside the edge.
  The Worker's `Date.now()` is reported but not trusted — the Workers runtime
  freezes its clock between I/O.
- Worker responses `cache-control: no-store`, so only KV/DO timing is measured.
- Fresh unique key per trial.
- Budget across both runs: ~79 writes + a few thousand reads/DO-requests — under
  10% of the free-tier daily KV allowance, and a separate bucket from D1.

## What this still does NOT claim

- One account, one time window, **default cacheTtl (60 s)**. The cacheTtl was not
  swept — a different `cacheTtl` would move the window directly.
- "No push-invalidation" is inferred from the ~53 s passive-expiry result, not
  from Cloudflare internals.
- 6 colos, 6 trials each — enough to show the clustering, not a global census.

## Relation to the Cloudflare docs

The KV docs already describe this behaviour qualitatively and correctly: changes
are "usually immediately visible ... at which they are made"; "may take up to 60
seconds or more to be visible in other global network locations"; take "longer in
locations which have recently read a previous version of a given key"; and "your
data is not sent automatically to every location's cache" — other locations update
"as their cached versions of the data time out." The default `cacheTtl` is 60 s.

So this is **not** a documentation correction and there is no clarification PR to
file. The contribution is the measured numbers behind the docs' qualitative "up to
60 seconds or more": in practice a cached cross-region reader lags by ~53 s (one
`cacheTtl`), clustered just under 60 s and never past it across 36 trials, while
the writing region is sub-second. The docs say "up to 60 s or more"; this says how
much, where, and why.

## For a developer, in one line

Your own Worker reads your write immediately; a cache in another region can serve
the old value for most of a minute; a `put` will not speed that up — only a shorter
`cacheTtl` will.

F-Keys LLC | www.f-keys.com
