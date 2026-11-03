# Production scan CLI

Actions runs the shared `ScanJobManager` directly, without FastAPI or the real-scan
acceptance script. The three dispatch inputs, run title, pinned Mihomo version,
artifact name, one-day retention, and schema v1 remain unchanged.

```bash
uv run python scripts/run_scan.py \
  --request-id <request-id> \
  --subscription-file <runner-temp/subscription-url> \
  --output-dir <runner-temp/action-artifact>
```

The subscription file contains one HTTP(S) URL, not subscription YAML. Keep it
private; the caller owns and removes it. The CLI never logs its contents and
downloads the subscription only once, then injects that exact in-memory snapshot
into the shared manager. Core configuration still uses `BEST_IP_MIHOMO_PATH` and
the existing `BEST_IP_*` limits. No test subscription secret is used in production.

`GITHUB_RUN_ID` and `GITHUB_RUN_ATTEMPT` supply positive run identity. Explicit
`--run-id`/`--run-attempt` must agree with those variables when present. Outside
Actions, use `--local-test` for synthetic identity (defaults 1/1); this flag is
rejected in Actions. Such artifacts are local acceptance data, not real runs.
Request IDs match `[A-Za-z0-9_-]{1,128}`. `--timeout` defaults to 1500 seconds and
must be positive and finite; its budget includes the subscription download.

Each invocation owns isolated temporary job/result directories. Timeout and
cancellation wait for manager shutdown. Cleanup failures take precedence over
the original failure and prevent publication. Unconfirmed workspaces are kept
for diagnosis rather than deleted from beneath a potentially running core.
After confirmed shutdown and temporary-directory cleanup, both JSON files are
written in a staging directory and published together. The output directory must
not already exist; user files are never overwritten or removed.

At least one success or partial node with validated exit evidence is required.
All-failed scans are not successful. Schema v1 `status.usable` retains its stricter
legacy meaning (no failed or partial nodes), so a valid partial artifact may have
`usable=false` while the CLI exits successfully.

| Exit | Safe code | Meaning |
| --- | --- | --- |
| 0 | `scan_completed` | Valid artifact published after cleanup |
| 2 | `input_invalid` | Arguments, identity, URL file, or subscription invalid |
| 3 | `environment_not_ready` | Core/configuration/runtime unavailable |
| 4 | `scan_unavailable` | No usable results or unexpected scan failure |
| 5 | `result_invalid` | Export, manifest, records, or credential check failed |
| 6 | `scan_timeout` | Budget expired; cancellation/shutdown awaited |
| 7 | `cleanup_failed` | Resource cleanup unconfirmed; no artifact published |
| 8 | `output_failed` | Destination exists or publication failed |
| 130 | `scan_cancelled` | Interrupted/cancelled; no artifact published |

Beyond the fixed code, a failing run prints one machine-readable line naming why
the subscription could not be used. `subscription_fetch_reason: <code>` covers the
CLI's own URL validation and download (`non_http_scheme`, `url_credentials`,
`localhost_target`, `dns_unresolved`, `dns_not_global`, `doh_unavailable`,
`dns_no_public_records`, `http_status_<status>`, `direct_timeout`, `direct_unreachable`,
`redirect_without_location`, `redirect_limit`, `response_too_large`, the `relay_*` codes
below, `unknown`);
`scan_failure_code: <code>` reports the manager's code when a job ends without a
ready manifest. Both are fixed tokens and never contain the subscription URL, its
host, or any credential.

set, the download is tried directly from the runner first and falls back to the Worker
subscription relay only when that direct attempt fails in a way that says "this egress
cannot reach the host" rather than "the subscription is wrong": `http_status_403` (the
observed signature of a host filtering by fetching network, seen in both directions — a
host that refuses Azure and a host that refuses Cloudflare, the latter with a Cloudflare
managed challenge), plus `direct_timeout` / `direct_unreachable` (the connection never
completed: blackhole, reset, TLS/protocol interruption). Every other failure is reported
as-is instead of being retried through the relay, so a wrong token (401) or a wrong path
(404) never turns into a second network round trip. A successful download prints the
egress that actually answered — `subscription_source: direct` or
`subscription_source: relay` — and adds
`subscription_fallback_reason: http_status_403|direct_timeout|direct_unreachable`
when the relay answered after the direct attempt failed. With neither variable set the
run is direct-only, and setting only one of the pair is a configuration error
(`environment_not_ready`). A failing download prints `subscription_fallback_reason`
before `subscription_fetch_reason` when the direct attempt had already failed. Once the
scan job settles (completed or not) the CLI prints one aggregate
`scan_timing: subscription=… scan_wall=… nodes=… peak_parallel=… node_p50=… node_p90=…
node_max=… retried=… failed=… phase_sum[…]` line to stderr: numbers only, no node names,
exit IPs or subscription content. Node times include discarded failed attempts;
`phase_sum` adds each phase across nodes, so under parallelism it exceeds the wall clock
and is read as proportions. Relay
failures report the fixed codes
`relay_url_invalid`, `relay_token_invalid`, `relay_unauthorized`,
`relay_response_too_large`, `relay_unreachable`, `relay_not_configured`,
`relay_timeout`, `relay_response_invalid`, `relay_empty_body` and `relay_error`. The
relay settles nothing about reachability: the CLI still re-validates every redirect
hop against the public-address rule.

Failures print only fixed safe codes/messages, never raw exception text. Offline
tests with synthetic fixtures prove structure and lifecycle, not public-network
Mihomo execution or Worker→Actions end-to-end acceptance.
