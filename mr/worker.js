// ============================================================
// KV-BENCH-MR v0.3.0  (cross-colo KV staleness + cacheTtl sweep)
// One regional-anchor Durable Object per region, two measurements:
//   1. KV cross-region staleness: write a key, then time how long readers pinned
//      to other regions keep serving the stale value after an overwrite.
//   2. cacheTtl sweep: read with an explicit cacheTtl so the driver can show the
//      stale window scales with (and is bounded by) the TTL set on get(), not just
//      the 60s default the original single-TTL run measured.
// F-Keys LLC | www.f-keys.com
// ============================================================
//
// CHANGE LOG v0.2.0 -> v0.3.0
// - RegionReader.readTtl(key, ttl): get(key, { cacheTtl: ttl }) with ttl clamped to
//   the documented floor of 30s; reports value + colo + the ttl actually used.
// - GET /mr/ctread?key&region=R&ttl=T: region-pinned read at an explicit cacheTtl,
//   so a region's edge caches the value for T seconds. Drives the cacheTtl sweep.
// - The staleness endpoints (/mr/write,/mr/prime,/mr/check) are unchanged.
//
// WORKFLOW STACK:
// 1. Gate every request on BENCH_TOKEN (X-Bench-Token header or ?token=).
// 2. POST /mr/write?key&val          -> env.BENCH.put(key, val) from the coordinator colo.
// 3. GET  /mr/prime?key&region=R     -> RegionReader(R).read(key): populate R's colo cache.
// 4. GET  /mr/check?key&region=R     -> RegionReader(R).read(key): R's current cached view.
// 5. GET  /mr/ctread?key&region&ttl  -> RegionReader(R).readTtl(key, ttl): cache at R for ttl s.
//    RegionReader reports the colo it actually runs in (cdn trace) so placement
//    is verified, never assumed.
//
// ASSET MANIFEST:
// - KV binding: BENCH
// - DO binding: READER -> class RegionReader (sqlite-class migration, no storage used)
// - Secret:     BENCH_TOKEN
//
// BOOT ORDER:
// - Stateless coordinator. RegionReader instances are regional execution anchors,
//   one reused per region.
// ============================================================

import { DurableObject } from "cloudflare:workers";

export class RegionReader extends DurableObject {
	// read: get the key from THIS DO's colo cache; report value + the colo we are in.
	async read(key) {
		try {
			var val = await this.env.BENCH.get(key);
			var where = await this.colo();
			return { val: val, colo: where };
		} catch (err) {
			console.error("RegionReader.read:", err);
			return { val: null, colo: "err" };
		}
	}

	// readTtl: get the key with an explicit cacheTtl so this colo caches the value
	// for `ttl` seconds. The floor is the documented minimum (30s); anything lower
	// is clamped. Reports value + colo + the ttl actually used.
	async readTtl(key, ttl) {
		try {
			var n = parseInt(ttl, 10);
			if (!(n >= 30)) { n = 30; }
			var val = await this.env.BENCH.get(key, { cacheTtl: n });
			var where = await this.colo();
			return { val: val, colo: where, ttl: n };
		} catch (err) {
			console.error("RegionReader.readTtl:", err);
			return { val: null, colo: "err", ttl: 0 };
		}
	}

	// colo: learn once which colo this DO runs in, via a cdn trace. Memoized in memory.
	async colo() {
		try {
			if (this.coloCache) { return this.coloCache; }
			var resp = await fetch("https://workers.cloudflare.com/cdn-cgi/trace");
			var text = await resp.text();
			var m = text.match(/colo=([A-Z]+)/);
			this.coloCache = m ? m[1] : "unknown";
			return this.coloCache;
		} catch (err) {
			console.error("RegionReader.colo:", err);
			return "unknown";
		}
	}
}

export default {
	async fetch(request, env) {
		try {
			var url = new URL(request.url);
			var path = url.pathname;

			var supplied = request.headers.get("x-bench-token") || url.searchParams.get("token") || "";
			if (!env.BENCH_TOKEN || supplied !== env.BENCH_TOKEN) {
				return json({ error: "forbidden" }, 403);
			}

			if (path === "/mr/write" && request.method === "POST") {
				var wkey = url.searchParams.get("key") || "";
				var wval = url.searchParams.get("val") || "";
				if (!wkey) { return json({ error: "missing key" }, 400); }
				await env.BENCH.put(wkey, wval);
				return json({ ok: true, serverTime: Date.now() });
			}

			if ((path === "/mr/prime" || path === "/mr/check") && request.method === "GET") {
				var rkey = url.searchParams.get("key") || "";
				var region = url.searchParams.get("region") || "";
				if (!rkey || !region) { return json({ error: "missing key/region" }, 400); }
				var id = env.READER.idFromName("reader-" + region);
				var stub = env.READER.get(id, { locationHint: region });
				var out = await stub.read(rkey);
				return json({ region: region, colo: out.colo, val: out.val, serverTime: Date.now() });
			}

			if (path === "/mr/ctread" && request.method === "GET") {
				var ckey = url.searchParams.get("key") || "";
				var cregion = url.searchParams.get("region") || "";
				var cttl = url.searchParams.get("ttl") || "";
				if (!ckey || !cregion || !cttl) { return json({ error: "missing key/region/ttl" }, 400); }
				var cid = env.READER.idFromName("reader-" + cregion);
				var cstub = env.READER.get(cid, { locationHint: cregion });
				var cout = await cstub.readTtl(ckey, cttl);
				return json({ region: cregion, colo: cout.colo, val: cout.val, ttl: cout.ttl, serverTime: Date.now() });
			}

			return json({ error: "not found" }, 404);
		} catch (err) {
			console.error("fetch:", err);
			return json({ error: "internal" }, 500);
		}
	}
};

// json: never cached, so only KV/DO timing is measured, never an HTTP cache.
function json(obj, status) {
	return new Response(JSON.stringify(obj), {
		status: status || 200,
		headers: { "content-type": "application/json", "cache-control": "no-store" }
	});
}
