// ============================================================
// KV-BENCH-MR v0.2.0  (multi-region cross-colo staleness)
// Writes a KV key from the coordinator colo, then times how long readers
// pinned to different regions (Durable Objects + locationHint) keep serving
// the stale value after an overwrite. Measures the real cross-region window
// behind Workers KV's documented "up to 60 seconds".
// F-Keys LLC | www.f-keys.com
// ============================================================
//
// WORKFLOW STACK:
// 1. Gate every request on BENCH_TOKEN (X-Bench-Token header or ?token=).
// 2. POST /mr/write?key&val       -> env.BENCH.put(key, val) from the coordinator colo.
// 3. GET  /mr/prime?key&region=R  -> RegionReader(R).read(key): populate R's colo cache.
// 4. GET  /mr/check?key&region=R  -> RegionReader(R).read(key): R's current cached view.
//    RegionReader reports the colo it actually runs in (cdn trace) so placement
//    is verified, never assumed.
//
// ASSET MANIFEST:
// - KV binding: BENCH
// - DO binding: READER -> class RegionReader (sqlite-class migration, no storage used)
// - Secret:     BENCH_TOKEN
//
// BOOT ORDER:
// - Stateless coordinator. One RegionReader instance per region, pinned by
//   locationHint; each is just a regional execution anchor for env.BENCH.get.
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
