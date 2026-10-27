#!/usr/bin/env python3
"""Create a Zenodo deposition for this repository, and STOP before publishing.

    python -I deposit.py            # create or update the draft
    python -I deposit.py --publish  # mint the DOI, irreversible

WHY IT DOES NOT PUBLISH BY DEFAULT
A published Zenodo record cannot be withdrawn. The DOI is permanent, it
resolves forever, and the metadata is harvested by indexers within the day. So
this builds the draft, uploads the archive, prints the link to read it in a
browser, and exits. Publishing is a separate explicit act by a person who has
read the thing.

WHAT IT UPLOADS
A source archive of the tagged release taken from `git archive`, so the bytes
deposited are the bytes the tag names rather than whatever is in the working
directory. The findings document goes up alongside it as a separate file, so
the result is readable from the record page without downloading anything.

ZENODO_TOKEN must be in the environment. It is never printed.
"""
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://zenodo.org/api"
TOKEN = os.environ.get("ZENODO_TOKEN")
if not TOKEN:
    sys.exit("ZENODO_TOKEN is not set")

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = "v0.2.0"
ARCHIVE = os.path.join(HERE, "kv-bench-%s.zip" % TAG)
PUBLISH = "--publish" in sys.argv

METADATA = {
    "metadata": {
        "upload_type": "software",
        "title": ("KV-Bench: measured read-after-write and cross-region "
                  "staleness for Cloudflare Workers KV"),
        "creators": [{
            "name": "Gonzalez, Vincent",
            "orcid": "0009-0005-3640-014X",
        }],
        "license": "mit",
        "version": TAG,
        "language": "eng",
        "keywords": [
            "Cloudflare Workers KV", "eventual consistency",
            "read-after-write", "cacheTtl", "edge cache", "Durable Objects",
            "cross-region", "serverless", "reproducible measurement",
        ],
        "related_identifiers": [{
            "identifier": "https://github.com/vince-gonzalez/kv-bench",
            "relation": "isSupplementTo",
            "scheme": "url",
        }],
        "description": """
<p>Cloudflare Workers KV is documented as eventually consistent, "up to 60
seconds or more" to propagate to other locations. That is a ceiling, not a
measurement. This harness measures the distribution behind it, from both sides,
on the free tier.</p>

<p><b>In the region that issued the write, read-after-write is sub-second.</b>
50 of 50 fresh keys were readable on the first poll (median 149&nbsp;ms), and 8
of 8 overwrites were visible on the first poll (median 122&nbsp;ms). A client
never waits to read back its own write locally.</p>

<p><b>In a region holding a previously cached value, the stale read lasts about
one full cacheTtl.</b> Six Durable Objects pinned by <code>locationHint</code>
to six distinct colos on five continents (Atlanta, San Jose, Madrid, Vienna,
Singapore, Melbourne, verified by trace) were each primed with the old value,
then timed after an overwrite. Across 36 trials the stale window was tightly
clustered near 53&nbsp;seconds (pooled median 52.9&nbsp;s, maximum 54.9&nbsp;s)
and never reached 60. A <code>put</code> does not push-invalidate remote caches,
so the window is passive cache expiry, not propagation latency; the small
per-region spread tracks prime order, not geography.</p>

<p><b>This is a measurement, not a documentation correction.</b> The KV docs
already describe this behaviour qualitatively and correctly. The contribution is
the numbers behind "up to 60 seconds or more": how much, where, and why.</p>

<p><b>Method.</b> Every trial is timed from the driver's wall clock outside the
edge, because the Workers runtime freezes <code>Date.now()</code> between I/O and
an in-isolate timer cannot measure this. Worker responses are sent
<code>no-store</code> so only KV timing is measured. A fresh unique key is used
per read-after-write trial. Stated limits: one account, free tier, one time
window, default <code>cacheTtl</code> (60&nbsp;s), which was not swept; six
colos, six trials each.</p>
""".strip(),
    }
}


def req(method, path, body=None, headers=None, raw=None):
    url = path if path.startswith("http") else API + path
    sep = "&" if "?" in url else "?"
    url = "%s%saccess_token=%s" % (url, sep, TOKEN)
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt[:500]}


def say(label, value):
    print("  %-22s %s" % (label, value))


# --- the archive is built from the TAG, not the working directory ------
subprocess.run(["git", "archive", "--format=zip",
                "--prefix=kv-bench-%s/" % TAG,
                "-o", ARCHIVE, TAG],
               cwd=HERE, check=True)
say("archive", "%s (%d bytes, from tag %s)"
    % (os.path.basename(ARCHIVE), os.path.getsize(ARCHIVE), TAG))

# --- reuse an existing unpublished draft rather than making a second --
status, mine = req("GET", "/deposit/depositions?status=draft&size=100")
draft = None
if status == 200 and isinstance(mine, list):
    for d in mine:
        if d.get("title", "").startswith("KV-Bench:"):
            draft = d
            break

if draft:
    say("existing draft", draft["id"])
    dep_id = draft["id"]
    status, body = req("PUT", "/deposit/depositions/%s" % dep_id, METADATA)
    if status not in (200, 201):
        sys.exit("  metadata update failed: %s %s" % (status, body))
else:
    status, body = req("POST", "/deposit/depositions", METADATA)
    if status not in (200, 201):
        sys.exit("  could not create the deposition: %s %s" % (status, body))
    draft = body
    dep_id = body["id"]
    say("created draft", dep_id)

status, draft = req("GET", "/deposit/depositions/%s" % dep_id)
bucket = draft["links"]["bucket"]

# --- upload, replacing any same-named file from an earlier attempt ----
for existing in draft.get("files", []):
    req("DELETE", "/deposit/depositions/%s/files/%s"
        % (dep_id, existing["id"]))

for path, name in [(ARCHIVE, os.path.basename(ARCHIVE)),
                   (os.path.join(HERE, "results", "FINDINGS.md"),
                    "FINDINGS.md")]:
    with io.open(path, "rb") as fh:
        blob = fh.read()
    st, resp = req("PUT", "%s/%s" % (bucket, name), raw=blob,
                   headers={"content-type": "application/octet-stream"})
    say("uploaded" if st in (200, 201) else "UPLOAD FAILED %s" % st,
        "%s (%d bytes)" % (name, len(blob)))

status, draft = req("GET", "/deposit/depositions/%s" % dep_id)
print()
say("state", draft.get("state"))
say("submitted", draft.get("submitted"))
say("files", ", ".join(f["filename"] for f in draft.get("files", [])))
say("reserved DOI", draft.get("metadata", {}).get(
    "prereserve_doi", {}).get("doi", "(assigned on publish)"))
print()
print("  READ IT HERE before anything is permanent:")
print("    %s" % draft["links"].get("html", "(no html link returned)"))

if not PUBLISH:
    print()
    print("  Not published. The draft is private to the account and nothing")
    print("  has a DOI yet. To mint it:  python -I deposit.py --publish")
    sys.exit(0)

print()
print("  PUBLISHING - this is irreversible")
status, body = req("POST", "/deposit/depositions/%s/actions/publish" % dep_id)
if status not in (200, 202):
    sys.exit("  publish failed: %s %s" % (status, body))
say("published DOI", body.get("doi"))
say("record", body.get("links", {}).get("record_html"))
