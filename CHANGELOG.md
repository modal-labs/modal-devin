# Changelog

All notable changes are documented here. The project follows Keep a Changelog
and uses semantic versioning.

## Unreleased

### Added

- `modal-devin init` and `OutpostsClient.create_outpost` now validate outpost names locally
  (lowercase letters, digits, dashes, underscores) before calling the API; interactive runs
  re-prompt instead of failing.

## 0.1.13 - 2026-09-16

### Fixed

- The `devin` CLI renamed `worker start --pool` to `--outpost`, which made the
  worker image's build-time contract check fail (`modal deploy` broke during the
  image build) and would have made the worker command itself fail at runtime.
  Both the contract check and the `sandbox.exec` worker command now use
  `--outpost`.

### Changed

- The default API URL is now `https://api.devin.ai` (Outposts left beta;
  `https://api.beta.devinenterprise.com` is no longer the default). Explicit
  `api_url` / `DEVIN_API_URL` / `--api-url` overrides are unchanged.

## 0.1.12 - 2026-07-19

### Changed

- Workers now notice and recover from a stuck attach much faster: the
  `session_attach_timeout_seconds` default dropped from 120s to 20s, and
  `status_watchdog_interval_seconds` from 60s to 10s. In practice a healthy
  session attaches within milliseconds, so there was no benefit to waiting
  two minutes before giving up and redispatching. Both remain configurable via
  `MODAL_DEVIN_SESSION_ATTACH_TIMEOUT_SECONDS` /
  `MODAL_DEVIN_STATUS_WATCHDOG_INTERVAL_SECONDS`.

## 0.1.11 - 2026-07-19

### Added

- Workers now enforce an attach timeout. A remote can connect to the outpost
  gateway and hold a perfectly healthy websocket that Devin's side never attaches
  to, leaving the session stuck at "your outpost machine is connecting" until a
  human intervenes — only a fresh connection recovers it. The worker watches its
  own log stream for the gateway connection and the first RPC; if Devin has not
  attached within `session_attach_timeout_seconds`
  (`MODAL_DEVIN_SESSION_ATTACH_TIMEOUT_SECONDS`, default 120 seconds) of
  connecting, the invocation fails with `SessionAttachTimeoutError`, the claim is
  released, and the scheduler redispatches a fresh worker whose new connection
  attaches. Reconnects restart the clock.

### Fixed

- The `devin.session.id` span attribute is no longer redacted by Logfire's default
  scrubber (which flags any attribute name matching "session"). Session IDs are
  identifiers, not credentials, and are the primary key for correlating a session's
  trace; other scrubber matches are still redacted.

## 0.1.10 - 2026-07-18

### Removed

- The Caddy credential-boundary sidecar. In the direct-serve hand-off the Devin CLI
  never contacts the queue API from inside the Sandbox, so the sidecar proxied
  nothing but its own health check; the service user token now never enters the
  Sandbox in any form (the CLI's token presence check is satisfied with an inert
  placeholder, and the remote authenticates with its session-scoped connect token).
  Workers start faster with no sidecar image build or readiness wait, and the
  `sidecar_ready_timeout_seconds` setting
  (`MODAL_DEVIN_SIDECAR_READY_TIMEOUT_SECONDS`) is removed; passing it to
  `WorkerSettings` is now an error.

### Known issues

- Serving sessions through the Devin CLI's documented claim mode (rather than the
  connect-token direct-serve hand-off) is blocked upstream: `devin` CLI 3000.1.27
  cannot parse claim responses from the Outposts API — including a fresh claim of
  a pending session — and exits with "failed to parse claim response". Direct-serve
  remains the serve path until a fixed CLI is released.

## 0.1.9 - 2026-07-18

### Added

- `modal-devin destroy` safely stops the Modal apps declared by one or all generated
  outpost files before deleting their Devin outposts. It requires confirmation,
  preserves local files and shared secrets, and reports partial failures.
- Running workers now poll their session's queue status and shut down once the
  session terminates or leaves the queue, instead of idling until the function
  timeout. The interval is configurable via `status_watchdog_interval_seconds`
  (`MODAL_DEVIN_STATUS_WATCHDOG_INTERVAL_SECONDS`, default 60 seconds).
- The scheduler and session runtimes emit OpenTelemetry traces through Pydantic
  Logfire. Every scheduler poll starts a trace that each dispatched session
  continues across the Modal function boundary through claim acquisition, Sandbox
  and sidecar startup, the Devin worker process, finalization, and claim release.
  Add `LOGFIRE_TOKEN` to the shared Modal Secret to send traces to Logfire, or set
  `OTEL_EXPORTER_OTLP_ENDPOINT` for any other OpenTelemetry-compatible backend.

### Fixed

- The scheduler no longer dispatches workers for queue entries whose session is
  suspended or already finished. Releasing a just-suspended session returns its
  entry to the pending phase until Devin prunes it, and dispatching on the phase
  alone claimed the sleeping session and produced an idle sandbox.
- Workers now boot the devin-remote binary the session pins
  (`spec.remote_binary_sha`) by passing `DEVIN_WORKER_REMOTE_SHA` to the Devin
  CLI. Because the worker serves the session directly (no queue API), the CLI
  never read the pin and booted the latest published remote instead; when
  Cognition ships a new remote, sessions pinned to it never attach and stall at
  "Your outpost machine hasn't connected".

## 0.1.8 - 2026-07-17

### Changed

- The default scheduler polling window is now 3 seconds, reducing the delay before
  queued sessions are picked up.

## 0.1.7 - 2026-07-16

### Fixed

- The default Devin API URL is restored to
  `https://api.beta.devinenterprise.com`; explicit `api_url` overrides remain
  supported.

## 0.1.6 - 2026-07-16

### Fixed

- Worker images and session startup now target the current Devin CLI contract:
  `--pool` identifies the outpost, and the claim's connect token is handed to the CLI
  so it does not claim the same session twice. This replaces the unsupported
  `--outpost` flag introduced in 0.1.5.

## 0.1.5 - 2026-07-16

### Fixed

- Every Modal session invocation now uses its own acceptor ID, so concurrent or
  duplicate Sandboxes cannot renew, steal, or release one another's claims.
- Final-state checks now retry successful but stale `pending`, `claimed`, or `running`
  responses before deciding recovery is required, matching the Outposts propagation
  contract.
- Claimed sessions are released after both confirmed completion and failure. Startup
  waits are bounded by the server-assigned claim deadline so an expired attempt does
  not continue provisioning in parallel with its replacement.
- Queue listing now paginates and deduplicates sessions at page boundaries, and final
  state checks use the single-session resource instead of a shared-acceptor list.
  Pagination tracks all cursors and rejects cycles or excessive page counts.

### Changed

- The default API URL is now `https://api.devin.ai`.
- The default worker duration is 12 hours instead of 30 minutes, and the default
  scheduler interval is restored to 30 seconds to reduce full-queue polling load.
- Worker startup now follows the documented Devin CLI contract: `--session`,
  `--outpost`, and the same unique `--acceptor-id` used for the API claim. Worker
  image builds verify those flags before deployment, failing during the build if an
  unpinned CLI update is incompatible.
- Independent schedulers may target the same outpost. Dispatch leases reduce
  duplicate Modal invocations locally, while the Outposts atomic claim decides which
  invocation serves each session.

### Removed

- The custom per-outpost ownership lock and per-session retry circuit breaker. Claim
  conflicts, expiration, and release now provide the documented fleet coordination
  and recovery behavior without a second ownership system.

## 0.1.4 - 2026-07-16

### Fixed

- `modal-devin deploy` now explicitly selects Modal's rolling deployment strategy,
  preserving active session invocations while a new application version takes over.

### Changed

- The generated scheduler's default poll interval (`scheduler_interval_seconds` /
  `MODAL_DEVIN_SCHEDULER_INTERVAL_SECONDS`) is now 3 seconds instead of 30, so
  queued sessions are picked up much sooner. This means roughly 10x more scheduler
  invocations per outpost by default; override it back up if invocation volume
  matters more than pickup latency for your deployment.

## 0.1.3 - 2026-07-16

### Fixed

- Workers no longer pre-claim a session and then hand it to `devin worker start`,
  which made the CLI claim the same session a second time with the same acceptor;
  the beta API's response to that redundant claim isn't one the current `devin`
  CLI can parse, so the worker exited immediately, released the claim, and the
  scheduler redispatched it forever. `Worker.run_session` now passes the
  `connect_token` and `gateway_url` from its own claim to `devin worker start` via
  `DEVIN_REMOTE_SESSION_TOKEN`/`DEVIN_OUTPOST_GATEWAY_URL`, the documented hand-off
  for a caller that has already claimed the session, so `devin-cli` runs the
  remote directly instead of claiming again.
- The worker's logger never had a handler attached, so every `logger.info()` call
  (claim/release/snapshot events, and the `devin worker start` subprocess's own
  stdout) was silently dropped instead of reaching Modal's logs.
- The sidecar's Caddyfile now enables structured access logging, so a session's
  HTTP traffic through the sidecar is visible in `modal app logs` alongside the
  worker's own log lines.

## 0.1.2 - 2026-07-16

### Added

- `modal-devin deploy` deploys every `*.py` file in `--outposts-dir` (default
  `outposts/`) when `OUTPOST-FILE` is omitted, continuing past a failed deployment
  and reporting every outpost that failed to deploy at the end.
- `modal-devin doctor` now checks that every outpost file in `--outposts-dir` has a
  matching deployed Modal app, reading each file's `Worker.from_env(...)` call to
  determine its expected app name without executing the file.

## 0.1.1 - 2026-07-16

### Changed

- `pool`/`pool_id` terminology is renamed to `outpost`/`outpost_id` throughout the
  public API, CLI, and generated files, matching Devin Outposts' current API
  vocabulary: `Worker(pool_id=...)` is now `Worker(outpost_id=...)`, the `init`
  `--pool-id` flag is now `--outpost-id`, and the default `init` output directory
  is `outposts/` instead of `pools/`.
- `modal-devin init` now creates and rolls back outposts with a direct call to the
  Devin Outposts API (`POST`/`DELETE /opbeta/outposts`) instead of shelling out to
  the `devin` CLI's `worker pool create`. The local `devin` CLI is no longer
  required to run `init`, `deploy`, or `doctor` -- it's still required inside the
  deployed worker image at runtime.

### Fixed

- `modal-devin init`'s outpost creation no longer returns `405 Method Not Allowed`
  against the Devin Outposts beta API: the `devin` CLI's `worker pool create`
  subcommand targeted a route the API backend had already retired in favor of
  `POST /opbeta/outposts`.
- The `init` rollback path's outpost-deletion request was hitting a nonexistent URL
  (`/outposts/pools/{id}`, missing the `/opbeta` prefix) and silently failing every
  time; it now calls the correct `DELETE /opbeta/outposts/{outpost_id}`.
- Worker image builds no longer fail on the `devin` CLI's own `install.sh`, whose last
  step unconditionally runs an interactive `devin setup` OAuth wizard that always
  fails without a TTY. The install step now succeeds or fails based on whether the
  `devin` binary is actually present and executable afterward, not on that wizard's
  incidental exit code -- a genuinely failed install still fails the build.

## 0.1.0 - 2026-07-16

### Added

- `Worker` as an immutable configured runtime with durable image, session, and
  dispatch operations.
- Validated `WorkerSettings` with explicit `MODAL_DEVIN_*` environment loading.
- Typed public exceptions for configuration, compatibility, API, status, and
  worker-process failures.
- `modal-devin init`, `deploy`, and `doctor` project-level commands.
- Modal Dict-backed filesystem snapshot persistence.

### Changed

- Generated files now own the `modal.App`, secret, image composition, function
  decorators, schedule, and connection between the scheduler and session function.
- Session and scheduler bodies are thin adapters into `Worker.run_session()` and
  `Worker.dispatch_pending_sessions()`.
- Claims are acquired inside the spawned session invocation rather than before the
  asynchronous Modal dispatch, so queue delays and function retries do not reuse a
  stale claim.
- Outposts session states are parsed into a closed protocol type; malformed or new
  states preserve recovery data instead of being treated as completed.
- Scheduler functions use a lightweight controller image instead of the full Devin
  worker image.
- Sidecar image cache entries are versioned by the sidecar recipe digest.
- `Worker.run_session()` inherits Modal Sandbox keyword typing through `ParamSpec`
  while protecting runtime-owned lifecycle options.
- Deployed functions are named `scheduler` and `session` by operational role.
- Worker images remain composable between `Worker.base_image()` and the terminal
  `Worker.prepare_image()` source mount.
- Final session status failures preserve a recovery snapshot and fail the Modal
  invocation.
- Nonzero Devin worker exits now raise `WorkerExitedError`.
- Outer function timeouts now account for status requests and backoff, sidecar
  readiness, snapshot creation, claim release, and cleanup.
- The Devin worker command receives the configured session timeout directly, while
  the containing Sandbox receives a larger lifecycle budget for startup and recovery.
- Scheduler transport, protocol, image-build, and dispatch failures now fail the
  scheduled invocation after attempting every independent dispatch.
- Pending session dispatches use bounded leases to avoid duplicate Modal queue entries.
- Snapshot index entries are refreshed daily while the scheduler is deployed, keeping
  their retention aligned with 30-day or indefinite filesystem snapshots.
- Package installation is documented as a project dependency rather than an
  isolated tool installation.
- Scheduler invocations now surface protocol, sidecar-build, and dispatch failures;
  all pending sessions are still attempted before dispatch errors are reported.
- Devin CLI installation failures now stop worker image builds at the failing
  installer step.

### Removed

- The `Worker.app()` application factory and its Modal function-option mappings.
- The low-level public `poll_and_dispatch()`, `build_sidecar_image_id()`,
  `SessionRunner`, and `OutpostPoolConfig` surface.
- The redundant `modal-devin outpost` command group.
- The `clone_private_repo()` image-build helper.

### Security

- Interactive Modal Secret creation now uses the public Modal SDK directly, keeping
  the Devin service key in memory instead of writing a temporary JSON file or
  invoking a secret-creation subprocess.
- Secret discovery now uses the public Modal SDK instead of parsing subprocess JSON,
  and credential setup documentation no longer stages tokens in local files.
- The sidecar health check is served locally and no longer sends a token-bearing
  request to the upstream API.
- The credential-injecting proxy only forwards Outposts API paths.
- Worker output combines stdout and stderr so failures remain observable.
