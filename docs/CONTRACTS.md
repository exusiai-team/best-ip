# Runtime contracts

This describes the local API, production CLI and Worker gateway contracts.
Fixtures are synthetic, credential-free examples, not evidence of a real scan.
`records.json` has a base node plus shallow case overrides; both languages
materialize the same records. The documentation IP is deliberately non-production.

## Entrypoints and boundaries

- Local: `uv run --no-dev python scripts/dev.py` (also `npm run dev`), optional
  `--port`, `--frontend-port`, `--no-open`, `--no-reload`. Separate loopback
  services default to frontend 5173 and API 8000 and select free ports independently.
  `/site-config.js` supplies `window.BEST_IP_CONFIG` with local mode and actual API base.
- Backend: `uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000`.
  FastAPI `/` is 404. Local CORS allows only configured frontend Origin, methods
  GET/POST/DELETE, headers Accept/Content-Type, and no credentials.
  Windows launcher disables reload; health checks file existence, not process creation.
- Production: Worker Static Assets + password session -> GitHub App -> fixed
  `silverwolfxai/best-ip`, `main`, `.github/workflows/scan.yml`.
  The workflow decrypts with `scripts/decrypt_subscription.mjs`, then runs
  `scripts/run_scan.py` directly against the shared scan core. No HTTP server or
  acceptance script runs in production. See [CLI.md](CLI.md) for arguments and exits.
- `npm run dry-run` checks bundling read-only. Releases only come from Cloudflare
  Workers Builds on `main` (`npx wrangler@latest deploy`, no install step). `npm run deploy`
  is a local tripwire: `scripts/deploy_guard.mjs` exits 2 outside Workers Builds. `.gitattributes` pins every text file to LF.
  Neither unit tests nor dry-run prove real Mihomo or Actions execution.

## Local HTTP

| Method/path | Request | Successful response |
| --- | --- | --- |
| GET `/api/health` | none | 200 `{status:"ok", mihomo_ready:boolean, mode:"local" or "runner-api"}` |
| POST `/api/scans` | JSON `{subscription_url, subscription_sha256?, request_id?}` | 202 `{id,status:"queued"}` |
| GET `/api/scans/{id}` | none | 200 job snapshot with result summaries |
| GET `/api/scans/{id}/export` | none | 200 full export with records and manifest |
| GET `/api/scans/{id}/results/{index}` | none | 200 full node record |
| DELETE `/api/scans/{id}` | none | 200 cancellation/job snapshot |

URL string length is 8..4096; optional digest is lowercase SHA-256 hex;
request IDs match `[A-Za-z0-9_-]{1,128}`. Local submission is plaintext only to
loopback and uses `credentials: omit`; it does not use the gateway token.
Errors use `{detail}`: validation 422 (safe generic text), duplicate 409,
Mihomo-not-ready 503, missing job/result 404, premature export/result 409,
and cancellation cleanup failure 500.

Job snapshots expose `id,status,message,created_at,finished_at,total,skipped,
completed,success_count,partial_count,failed_count,current_node,manifest_ready,
cleanup_confirmed,execution_mode,error,results`. Status values include queued,
preparing, running, completed, failed, cancelled. Persisted progress uses
`job_id` and `phase` (currently human-readable message), not HTTP `id/message`.
Lifecycle fixtures describe minimum failure/cancellation facts; structured scan
errors are defined in `backend/app/scan/errors.py`.

## Gateway HTTP and dispatch

All requests, including static assets/config and health, require a password session,
except GET/POST `/login`, GET/POST `/logout`, and GET `/login.css`. The subscription
relay in the next paragraph is a separate server-to-server endpoint guarded by its own
shared secret instead of the browser session.
Login accepts a URL-encoded password form (8192-byte cap), returning a 303 to
`/` and a 12-hour signed `__Host-best-ip-session` cookie with Secure,
HttpOnly, SameSite=Strict and Path=/. Wrong passwords return 401. Passwords
must contain 16–1024 characters. Changing SITE_PASSWORD or SCAN_TOKEN_SECRET
invalidates existing sessions. POST logout clears the current browser cookie.
Login and logout POSTs require same-origin metadata. Unauthenticated HTML
navigation redirects to `/login`; API requests return 401. Authentication
responses and protected assets use Cache-Control: no-store.
Health does not require a scan token. POST scans requires a session plus same-origin
Origin/Fetch-Metadata but does not require a pre-existing scan token. Subsequent
GET/DELETE/artifact requests require `X-Best-IP-Scan-Token`, an in-memory,
two-hour token bound to request ID and dispatch time. Mutations are same-origin.

- GET `/api/health`: `{status:"ok",mode:"github-actions-gateway",authentication:"password"}`.
- POST `/api/scans`: `{request_id,key_id,envelope}`; returns 202
  `{request_id,run_id:null,dispatched_at,scan_token}`.
- Envelope v1: `v,kid,alg,request_id,issued_at,expires_at,ek,iv,aad,ct`;
  algorithm `RSA-OAEP-3072-SHA256+AES-256-GCM`. AES-256-GCM encrypts the URL,
  RSA-OAEP wraps the key; public-key SPKI SHA-256 must match key ID.
- Dispatch body: `{ref:"main",inputs:{request_id,key_id,
  encrypted_subscription_url:JSON.stringify(envelope)}}`.
  Workflow input names/types remain three required strings.
- GET `/api/scans/{request_id}?run_id={id}&run_attempt={attempt}`: request ID,
  normalized status, run, jobs, jobs availability/count/warning, artifact readiness
  and (once resolved) artifact ID/name. Unresolved run is `dispatching`, run null,
  jobs empty, job total null. Successful run without artifact is `artifact_pending`.
  `progress` is the runner's latest live reading for this exact run and attempt —
  `{phase,total,completed,success,partial,failed,updated_at}` with `phase` one of
  `subscription|scanning|packaging|done` — or null when none was reported (or the
  progress store is unbound). Here it carries counts only (the per-node reading is
  served by `/progress` below); the table and every exported number still come
  exclusively from the verified artifact.
- GET `/api/scans/{request_id}/progress?run_id={id}&run_attempt={attempt}`: session
  only, no scan token (a reloaded page following `active` has none). Both query
  parameters are required (400 `invalid_request` otherwise). Reads the Durable Object
  only — no GitHub API call — so the page asks once per second. Returns
  `{request_id,run_id,run_attempt,progress}` where `progress` is the same reading as above
  plus `nodes` (or null when absent / the run or attempt does not match): one entry per
  node in subscription order, `{n,t,s,a,ms,r}` — display name, protocol type, state
  `wait|retry|start|connect|lookup|success|partial|failed`, highest attempt started,
  elapsed ms (whole node, retries included; for an active node up to the report),
  whitelisted reason or null.
- GET `/api/scans/latest`: session only, no scan token. Picks the newest completed run
  (up to five probes back) whose artifact still exists and returns
  `{request_id,status,run,artifact_ready,artifact_id,artifact_name,artifact_url,scanned_at,active}`;
  without a readable artifact `status` is `none`, `artifact_expired`, or the newest
  run's own state. `active` is the newest run while it is still queued/running —
  `{request_id,status,run,progress}` — so a reloaded page can follow it to completion;
  otherwise null. It exposes no jobs and grants no cancel right.
- GET `/api/scans/{request_id}/artifact?run_id={id}&run_attempt={attempt}`:
  `{artifact_id,artifact_name,artifact_url}` only after exact run and artifact checks.
  `artifact_url` must be an `https` URL on `*.blob.core.windows.net`; it is GitHub's
  short-lived signed location, and the browser fetches those bytes itself with a plain
  cross-origin GET that carries no custom headers (so no preflight). Cloudflare egress
  to that blob family hangs or is rewritten to 522 on a large share of requests, so the
  Worker never downloads the archive. A missing or foreign location is 502
  `GitHub artifact 下载地址缺失（...）` with `code: "artifact_location_missing"`.
- DELETE `/api/scans/{request_id}?run_id={id}`: 202
  `{request_id,run_id,status:"cancelled"}` means cancellation requested to GitHub,
  not proof of completed runner cleanup; unresolved run returns 409.
- Errors: `{error:stable_code,detail:safe_message}`, no-store; unexpected errors
  become generic `internal_error` 500.

- POST `/api/subscription-relay`: server-to-server subscription fetch, outside the
  password session and without same-origin checks, because the caller is an Actions
  runner with no cookie or Origin. It requires header
  `X-Best-IP-Relay-Token`, compared in constant time against the Worker secret
  `SUBSCRIPTION_RELAY_TOKEN`; a missing or short secret is 503 `worker_not_configured`
  and a wrong token is 401 `unauthorized`. Body `{url}` is validated structurally only
  (length 8..4096, http/https, hostname present, no userinfo, no whitespace or control
  characters, port 1..65535, and no localhost/`.localhost`/`.internal`/`.local` or
  private, loopback, link-local and reserved IP literals). The Worker therefore does
  NOT resolve DNS or judge the target's public reachability; the runner re-validates
  every hop with `validate_public_url`. The runner only reaches this route as a fallback:
  it downloads directly first and retries through the relay solely when that direct attempt
  fails as an egress problem — `http_status_403` (a host filtering by fetching network) or
  `direct_timeout` / `direct_unreachable` (connection never completed) — while every other
  failure is reported as-is, and a failing download prints `subscription_fallback_reason`
  before `subscription_fetch_reason`. Upstream is fetched with `redirect:"manual"`
  and a 20 second timeout on Cloudflare's egress, capped at 5 MiB. A 200 carries
  `{status,location,body_b64}`: `location` only for 300..399 (truncated to 4096 chars,
  with `body_b64:null`) so the runner can re-validate the hop, otherwise `body_b64` is
  base64 of the upstream body. Errors are `{error:stable_code}` and no-store: 400
  `invalid_request`, 405 `method_not_allowed`, 413 `response_too_large`, 502
  `upstream_unreachable`, 504 `upstream_timeout`. The Worker never echoes the target
  URL, never logs it, and forwards no inbound headers or cookies.

- POST `/api/scan-progress`: server-to-server progress report from the scan runner,
  outside the password session like the relay and authenticated by the same
  `X-Best-IP-Relay-Token` / `SUBSCRIPTION_RELAY_TOKEN` pair. Body (≤ 256 KiB)
  `{request_id,run_id,run_attempt,phase,total,completed,success,partial,failed,nodes?}`;
  every count is an integer 0..100000, `completed ≤ total` and
  `success+partial+failed = completed`. `nodes`, when present, has exactly `total`
  entries (≤ 1000) of `{n,t,s,a,ms,r}`: `n` 1..80 chars without control characters,
  `t` `[a-z0-9-]{0,24}`, `s` one of the eight states, `a` 0..10, `ms` 0..3600000,
  `r` null or 1..60 chars; the number of `success`/`partial`/`failed` entries must equal
  the three counts. Anything else is 400 `invalid_request`. Accepted reports
  return 204 and land in the `SCAN_PROGRESS` Durable Object (one instance per request ID,
  latest report only, purged six hours after the first write); a report for the same run
  that would move the phase backwards or lower `completed` is ignored. Missing binding is
  503 `worker_not_configured`. Reports never contain exit IPs, scores or subscription
  content: a node name that contains any subscription URL/credential value (the
  artifact's own forbidden set) is replaced by `节点 N`, and `r` is one of the fixed
  category messages (`backend/app/scan/errors.py` `live_reason` / `live_record_reason`).
  The runner reports roughly every 0.75 s when any node changes state (heartbeat 5 s),
  treats every failure as non-fatal and prints `progress_reports: sent=N failed=M` once
  at the end.

Run title is `Best IP scan {request_id}`. Identity checks bind title, workflow,
dispatch event, branch, creation window, run and attempt. Artifact name is
`best-ip-result-{request_id}-{run_id}-{run_attempt}`, unique and not expired;
upload retention is one day. Tests exercise actual Worker dispatch and lookup.

## Result and artifact v1

Disk layout: `{job_id}/progress.json`, `nodes/0000.json` onward, `manifest.json`.
Files are atomically written. Exactly one terminal record per actual node, with
status success/partial/failed. Failed records require a safe error and null exit IP.
Success and partial require a valid IP and confirmed workspace Mihomo proxy
evidence, loopback HTTP port, no trust_env, and five true base collection checks.
`ipure_scores` is always a seven-key map — `total` plus the six documented
scenarios `ai, social, streaming, gaming, ecommerce, email` — where every value is
an integer 0..100, the sentinel `-1`, or null, and `score` must equal
`ipure_scores.total`. `-1` means IPure marked that total/scenario as
`restricted` (region-blocked): the value is off the 0..100 scale, is rendered as
the number `-1` in neutral grey, and must not be read as "zero" or "lowest".
Nothing else is accepted — `-2` and `101` are rejected by the record validator.
Level codes, verdicts and per-scenario level maps are not part of the record:
they stay inside `requests.ipure.data` as raw upstream evidence, so the frontend
has no level text to render. `data.total` and `data.scenarios[].score` keep the
raw upstream numbers — `null` included — and the `-1` mapping exists only in the
record, so one document never mixes two conventions for one value.
Migration: earlier versions wrote `ipure_level`, `ipure_verdict` and
`ipure_scenario_levels`. Both validators now ignore those three names on read
(`_RETIRED_NODE_FIELDS`, `retired`) so results and exports written before the
change still open; they are never written again, and that tolerance can go once
`RUNTIME_DIR/results` holds no pre-change job.
IPure requires no API key or Cookie, so no fallback session evidence exists. A score may
come from the workspace proxy or, when the node egress cannot connect, from a recorded
host-side query: such a record sets `requests.ipure.direct_fallback:true`,
`via_mihomo:false`, and `proxy_evidence.ipure_via_direct_fallback:true`. Both validators
cross-check that trio, so a direct score can never be presented as proxy evidence.
Network identity is IPure-first. `requests.ipure.data.network` keeps what the
`/api/lookup` report itself stated, mapped from its documented shape: `ip`;
`geo.countryName` → `country`, `geo.country` → `country_code`, `geo.region` / `geo.city`;
`asn.asn` → `asn`, `asn.name` ("GOOGLE - Google LLC, US") → `as_name` ("Google LLC"),
`asn.org` → `org`; `registry.org` / `registry.country` → `registry_org` /
`registered_country`; `usageType` (documented enum residential · mobile · business ·
hosting · education · government · unknown) → `usage_type` (unknown and off-enum values
become null) + `is_residential` (true only for residential); `nativeType` (native ·
broadcast · unknown) → `native_type` + `is_native` (true / false / null).
Anything missing or mistyped is null. The record then fills five groups IPure-first and
falls back to the Coffee lookup only for a group IPure left empty: geo (`country`,
`country_code`, `region`, `city`, `location`), ISP (`isp` = `asn.org`, `as_org` = cleaned
AS name, `asn`), kind (`usage_type`, `is_residential`, `is_datacenter` = hosting), native (`is_native`,
`native_status`, `native_detail`). `network_source = {exit_ip, geo, isp, kind, native}`
(each `"ipure"`, `"coffee"` or null) says who supplied each group. `exit_ip` is still
discovered by the Coffee trace (an IPure query needs an address) and is marked `"ipure"`
when IPure's echoed `ip` equals it. The six new top-level fields are optional — artifacts
written before them still validate — and the record validator rejects a non-string geo field, a
`usage_type` outside the six documented values, or a `network_source` with other keys or
values.
Retry metadata, when present, must be internally consistent.
Required and optional record fields are enforced by Python's ResultStore validator;
fixtures carry the minimal valid set, not a real enrichment response.

Manifest: `schema_version:1,job_id,status:"completed",created_at,finished_at,
total,skipped,completed,all_records_present:true,complete:true,execution_mode,
counts:{success,partial,failed,complete},records`.
Each entry has `index,file,bytes,sha256,summary`; index/file are contiguous,
digest/size bind exact node bytes, summary matches the record and counts are derived.
Manifest `complete:true` means all records present, NOT all nodes usable.

Export contains completed job fields, cleanup confirmation, full `results` and
manifest. Artifact ZIP contains exactly `status.json` and `result.json` (export).
Status fields: `schema_version:1,sanitized:true,request_id,run_id,run_attempt,
status:"completed",usable,total,completed,counts,result_sha256`.
Digest covers exact UTF-8 result bytes, including formatting/newline.
Sanitizer rejects credential keys, credential-bearing URLs and subscription values.
Browser checks ZIP paths, duplicate entries, stored/deflate methods, CRC and bounded
sizes (50 MiB compressed, 100 MiB decompressed), digest, identity and aggregate facts.
A body shorter than 22 bytes is reported as `artifact ZIP 内容不完整：只收到 N 字节`
as well as a missing end-of-central-directory, both tagged `code: "artifact_truncated"`.
That code is the only read failure the gateway may refetch (bounded to three attempts
inside one poll); every other mismatch — including a complete archive whose manifest
disagrees with its records — still ends the scan on the first attempt.

Validation boundaries:

1. Record rules live in one place, `backend/app/results/validation.py`: failed
   records with an exit IP and successful records missing proxy evidence are
   rejected when a node is written, when the manifest is finalized and again by
   `build_artifact` before publishing. The browser reader
   (`frontend-next/app/artifact/reader.js`) checks integrity only — run identity,
   `result_sha256`, counts and manifest/record agreement. `tests/test_artifact_reader.py`
   feeds a runner-built artifact to that reader.
2. `status.usable` means zero failed AND zero partial nodes in sanitizer/browser.
   Partial artifacts are structurally accepted but usable=false. An all-failed
   manifest is structurally complete yet unusable; workflow success is not node success.
3. Environment failures and cancellations have no completed artifact in the current
   workflow. They are lifecycle samples, not fabricated terminal result archives.

## Environment variables (names only; no deployed values inspected)

| Scope | Variables/defaults |
| --- | --- |
| Local | `BEST_IP_MIHOMO_PATH` runtime/mihomo/mihomo(.exe); `BEST_IP_LOCAL_DEV` false; `BEST_IP_LOCAL_FRONTEND_ORIGIN` unset; `BEST_IP_OUTBOUND_INTERFACE` unset |
| Limits | `BEST_IP_SUBSCRIPTION_MAX_BYTES` 5242880; `BEST_IP_MAX_NODES` 500; `BEST_IP_MAX_PARALLEL_JOBS` 2; `BEST_IP_MAX_PARALLEL_NODES` min(16, max(8, CPU count)); `BEST_IP_MAX_NODE_ATTEMPTS` 3; `BEST_IP_NODE_RETRY_BACKOFF_MS` 500; `BEST_IP_PAGE_TIMEOUT_MS` 45000; `BEST_IP_SUBSCRIPTION_TIMEOUT_SECONDS` 30 |
| Download | `BEST_IP_MIHOMO_TAG`, `BEST_IP_MIHOMO_ARCHIVE_SHA256` optional command defaults; workflow pins v1.19.30 and archive SHA-256 |
| Local verifier | `BEST_IP_TEST_SUBSCRIPTION_URL`; `BEST_IP_API_BASE` http://127.0.0.1:8000; `BEST_IP_REAL_SCAN_TIMEOUT_SECONDS` 1800; `BEST_IP_REQUEST_ID`; `BEST_IP_ALLOW_PARTIAL` opt-in |
| Worker | `SCAN_KEY_ID`, `SCAN_TOKEN_SECRET`, `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`, `SITE_PASSWORD`, `SUBSCRIPTION_RELAY_TOKEN`; `ASSETS` binding |
| Actions | secrets `SCAN_PRIVATE_KEY_PEM`, `IPURE_CONFIG_YAML`, `SCAN_RELAY_TOKEN`; variables `SCAN_KEY_ID`, `SCAN_RELAY_URL`; temporary (step-scoped) `BEST_IP_ENVELOPE`, `REQUEST_ID`, `KEY_ID`, `EXPECTED_KEY_ID`, `RELAY_URL`, `RELAY_TOKEN`, `IPURE_CONFIG_YAML`, `SCAN_PRIVATE_KEY_PEM` (written to `$RUNNER_TEMP/scan-private.pem` and deleted as soon as the envelope is decrypted), `BEST_IP_SUBSCRIPTION_RELAY_URL`, `BEST_IP_SUBSCRIPTION_RELAY_TOKEN`; job-level `BEST_IP_PYTHON_DIR`, `BEST_IP_MIHOMO_ARCHIVE_DIR` (the cached interpreter and Mihomo archive); run identity from `GITHUB_RUN_ID` and `GITHUB_RUN_ATTEMPT` |
| Deployment (Workers Builds) | none configured; guard reads the injected `WORKERS_CI`, `WORKERS_CI_BRANCH` |

## Frontend transport boundary

Use exactly `health()`, `start(subscriptionUrl)`, `poll(session)`, `cancel(session)`.
The opaque session owns token/run/attempt/dispatch time. UI never persists those.
ScanSnapshot separately represents execution, phase/safe error, progress source,
unknown totals as null, result availability and terminal node counts.
Keep local JSON and gateway encrypted envelope/ZIP distinct below this interface.
Separate unavailable, verified-with-usable-nodes, all-failed and invalid results;
do not interpret old artifact `usable=false` as absence of a partial exit IP.
An intentional change to usable or validator rules needs explicit fixture
expectation changes and migration notes, not an incidental module move.
