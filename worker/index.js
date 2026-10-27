// ============================================================
// KV-BENCH v0.1.0
// Measures Cloudflare Workers KV read-after-write visibility latency.
// F-Keys LLC | www.f-keys.com
// ============================================================
//
// WORKFLOW STACK:
// 1. Gate every request on the BENCH_TOKEN secret (X-Bench-Token header or ?token=).
// 2. POST /write?key=K&val=V   -> env.BENCH.put(K, V); report server time.
// 3. GET  /read?key=K[&ttl=N]  -> env.BENCH.get(K, {cacheTtl:N}); report value + server time.
// 4. Anything else -> 404.
//
// ASSET MANIFEST:
// - KV namespace binding: BENCH          (wrangler.toml)
// - Secret:               BENCH_TOKEN     (wrangler secret put BENCH_TOKEN)
//
// BOOT ORDER:
// - Stateless. Each fetch() is independent; no module-global mutable state.
// - Responses are sent cache-control: no-store so the HTTP layer never masks
//   KV read timing. Worker-side Date.now() is reported but NOT the timing of
//   record (the Workers clock only advances on I/O); the driver times from
//   its own wall clock outside the edge.
// ============================================================

export default {
	async fetch(request, env) {
		try {
			var url = new URL(request.url);
			var path = url.pathname;

			// --- gate: an open KV-writing endpoint is an abuse/billing hole ---
			var supplied = request.headers.get("x-bench-token") || url.searchParams.get("token") || "";
			if (!env.BENCH_TOKEN || supplied !== env.BENCH_TOKEN) {
				return json({ error: "forbidden" }, 403);
			}

			// --- write ---
			if (path === "/write" && request.method === "POST") {
				var wkey = url.searchParams.get("key") || "";
				var wval = url.searchParams.get("val") || "";
				if (!wkey) { return json({ error: "missing key" }, 400); }
				await env.BENCH.put(wkey, wval);
				return json({ ok: true, key: wkey, serverTime: Date.now() });
			}

			// --- read ---
			if (path === "/read" && request.method === "GET") {
				var rkey = url.searchParams.get("key") || "";
				if (!rkey) { return json({ error: "missing key" }, 400); }
				var opts = {};
				var ttlRaw = url.searchParams.get("ttl");
				if (ttlRaw) { opts.cacheTtl = parseInt(ttlRaw, 10); } // KV minimum is 60
				var got = await env.BENCH.get(rkey, opts);
				return json({ key: rkey, val: got, serverTime: Date.now() });
			}

			return json({ error: "not found" }, 404);
		} catch (err) {
			console.error("fetch:", err);
			return json({ error: "internal" }, 500);
		}
	}
};

// json: build a JSON Response, never cached, so only KV timing is measured.
function json(obj, status) {
	return new Response(JSON.stringify(obj), {
		status: status || 200,
		headers: { "content-type": "application/json", "cache-control": "no-store" }
	});
}
