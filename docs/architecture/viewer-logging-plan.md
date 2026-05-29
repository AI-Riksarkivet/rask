# Viewer structured-logging plan (stdlib, OTel-ready)

Status: proposed. Scope: `components/services/viewer`. Owner preference: **simple, minimal, no
over-engineering.** This plan adds structured stdlib logging with request-id correlation and
nothing more.

## Goals and hard constraints

- **Stdlib `logging` only.** No `structlog`. No new heavy deps. The structured JSON formatter is a
  ~30-line custom `logging.Formatter` subclass living in the viewer (see "Why not python-json-logger"
  below). This matches the project standard repeated across the fastapi / python-infrastructure /
  writing-python skills.
- **OTel is OUT OF SCOPE now, but the design MUST be OTel-ready.** We do not wire any OpenTelemetry
  SDK, exporter, or instrumentor in this change. We only shape records so a *future* OTel
  `LoggingHandler` (via `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true` /
  `LoggingInstrumentor`) can forward them with **zero code change**. The OTel-ready contract is:
  (1) every log message is a **static snake_case event name**, (2) all variable data goes in
  `extra={...}` so it becomes a log-record attribute (and later an OTel span/log attribute), and
  (3) configuration happens once on stdlib `logging`. When OTel lands it attaches its handler to the
  root logger and harvests these same records — our formatter just stops being the only sink.
- **F-string rule, honored precisely.** The project default is f-strings for *string formatting*
  (error/exception text). For **log payloads** that rule does NOT mean "interpolate data into the
  message". The house pattern is: the log **MESSAGE** is a constant snake_case event name, and the
  variable data lives in `extra={...}`. So f-strings disappear from log *calls* (nothing left to
  interpolate) while remaining the default everywhere else. Lazy `%`-args are not used either — there
  is no dynamic message to defer.
- **Configure in lifespan startup, never at import.** No module-level `basicConfig`, no `dictConfig`
  at import time, and **not in `create_app()` either** — `main.py` runs `app = create_app()`
  unconditionally at module import (`main.py:74`), and every test does `from viewer.main import
  create_app`, so anything `create_app()` calls would rebuild `dictConfig` at import and once more per
  test app build. Importing `viewer.main` must not mutate global logging state beyond the
  module-level `log = logging.getLogger(__name__)` getters already in place. `configure_logging` is
  therefore called from **lifespan startup** (see §1) — matching the fastapi project-template's
  "startup work goes in lifespan" rule.

### Why not python-json-logger

A single small `logging.Formatter` subclass (`viewer/core/logging.py`, below) is *simpler* than
adding a dependency: it is one file, no version to track, and it lets us inject `request_id` and a
fixed key set without configuring a third-party field map. python-json-logger would not reduce code
here and would add a dep for ~25 lines. Decision: **custom formatter, no new dep.**

---

## 1. Where logging is configured and how

New file: **`components/services/viewer/src/viewer/core/logging.py`**

Contains three things and no import-time side effects:

- `RequestIdFilter(logging.Filter)` — a `filter()` that sets `record.request_id =
  current_request_id()` on every record (see §2). Attached to the handler so *all* records (app,
  uvicorn, third-party) carry the field.
- `JsonFormatter(logging.Formatter)` — `format(record)` builds a dict with a **fixed key set** and
  `json.dumps` it:
  `{"ts","level","logger","event","request_id", **allowlisted-extras, ["exc"]}` where
  `event = record.getMessage()` (the static event name), `level = record.levelname`,
  `logger = record.name`. When `record.exc_info` is set it appends the formatted traceback under
  `"exc"`. Extra fields land via a **fixed allowlist** of the keys this codebase actually emits —
  no per-record `__dict__` diffing against the stdlib `LogRecord` attribute set. The allowlist is a
  module-level tuple:
  ```python
  _EXTRA_KEYS = (
      "method", "path", "status", "duration_ms",
      "pipeline", "chunk_id", "chunk_total", "slot", "submission_id", "batch_count",
      "interval_seconds", "skipped_reason",
      "prefetch_eligible_count", "htr_eligible_count", "cooldowns",
      "cached_total", "transcribed_total", "rows_updated", "by_status",
      "driver_job_id", "url", "table", "error", "error_type",
  )
  ```
  `format()` copies only those keys that are present on the record (`for k in _EXTRA_KEYS: if k in
  record.__dict__: out[k] = record.__dict__[k]`). Deterministic, self-documenting, and one less
  failure mode than version-dependent reserved-name detection. There is **one formatter only** — see
  the note on `RASK_LOG_FORMAT` below.
- `configure_logging(settings: Settings) -> None` — builds a `logging.dictConfig` (single call,
  `disable_existing_loggers=False`) with:
  - one `console` handler → `StreamHandler` on stdout using `JsonFormatter`, with `RequestIdFilter`
    attached;
  - root logger at `settings.log_level` (validated — see config below);
  - `uvicorn` and `uvicorn.error` re-pointed at the same handler with `propagate=False` so uvicorn's
    own boot/error lines come out in our format instead of mixed plain text;
  - `uvicorn.access` **silenced** (level `WARNING`, `propagate=False`, no handler). We emit one
    app-side access line per request via `LoggingMiddleware` (§3); leaving `uvicorn.access` on would
    log a *second* access line per request in a different shape. Pick one — we pick ours, because it
    carries `request_id` and `duration_ms`.

> JSON to stdout is readable enough in dev and is the documented default, so we ship **one
> formatter** (`JsonFormatter`). No `ConsoleFormatter`, no `RASK_LOG_FORMAT` tunable, no second
> `dictConfig` branch. If human-readable dev output is ever actually needed, add it then.

New `Settings` field in **`core/config.py`** (RASK_* convention, env-tunable, **validated**):

```python
from typing import Literal

log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO", alias="RASK_LOG_LEVEL")
```

Using `Literal` (the writing-python configuration convention) makes a typo like `RASK_LOG_LEVEL=INFraO`
fail fast at `Settings` validation with a clear message, instead of silently no-opping or raising
deep inside `dictConfig`.

Call site: **lifespan startup** in **`core/lifespan.py`** calls `configure_logging(settings)` as the
**first statement** inside the `lifespan` context manager, before any `app.state` setup and before the
first app log line (`startup_complete`). This runs exactly once per real app run and is *not* invoked
at import — so `from viewer.main import create_app` (every test) does not mutate global logging state,
and tests that build many apps don't rebuild `dictConfig` N times. Before lifespan the only emitters
are uvicorn/FastAPI boot lines, which uvicorn configures itself; folding `uvicorn.*` into our handler
takes effect from `startup_complete` onward, which is sufficient.

---

## 2. Request-id correlation (ContextVar + logging.Filter)

New file: **`components/services/viewer/src/viewer/core/context.py`**

```python
from contextvars import ContextVar

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

def current_request_id() -> str:
    return request_id_ctx.get()
```

`RequestIdFilter.filter()` reads `current_request_id()` and stamps `record.request_id`. Because it is
a `ContextVar`, the id is asyncio-safe and reaches **every** record emitted during a request —
including ones from repos, services, and background helpers that never receive `request` as a
parameter (orchestrator-triggered code keeps the default `"-"`).

`RequestIDMiddleware.dispatch` (in `core/middleware.py`) is the single writer. It already mints/echoes
the id and sets `request.state.request_id`; we extend it to also set the ContextVar, with the
**critical reset rule**:

```python
async def dispatch(self, request, call_next):
    request_id = request.headers.get(_REQUEST_ID_HEADER) or uuid.uuid4().hex
    request.state.request_id = request_id
    token = request_id_ctx.set(request_id)
    try:
        response = await call_next(request)
        response.headers[_REQUEST_ID_HEADER] = request_id
        return response
    finally:
        request_id_ctx.reset(token)   # MUST reset, else ids leak across requests under worker reuse
```

This is the only change to the existing middleware behavior; `request.state.request_id` stays for any
code that prefers reading it directly. The exception handler in `core/exceptions.py` currently
discards the `Request` (`async def _domain(_: Request, ...)`) — no change needed: its `log.exception`
now picks up `request_id` from the ContextVar via the filter automatically.

---

## 3. Middleware order and the access-log middleware

Target registration order in `register_middleware` (onion: last-added runs first inbound, so CORS
ends up outermost):

1. `CORSMiddleware` (conditional on `settings.cors_origins`) — already exposes `X-Request-ID` and
   `X-Response-Time`.
2. `RequestIDMiddleware` — sets state + ContextVar (§2).
3. `TimingMiddleware` — sets `X-Response-Time` header.
4. **`LoggingMiddleware`** — NEW. One access line per request.

This matches the skill-mandated order **CORS → Request ID → Timing → Logging**.

**Yes, add the access-log middleware** (and silence `uvicorn.access` per §1 so there is exactly one
access line per request). Today no per-request line is emitted by the app, and uvicorn's default
access log has no `request_id`. The new `LoggingMiddleware` uses a named logger
`log = logging.getLogger("viewer.requests")` and folds in the wall time `TimingMiddleware` already
measures (recompute the `perf_counter` delta locally for the log's `duration_ms`; keep the header in
`TimingMiddleware`).

**Skip the noisy paths.** The otel/fastapi skills both call out excluding high-churn endpoints. The
SPA static mount (`/_app/*`, from `app.mount("/_app", StaticFiles(...))` in `main.py`) and the Ray
dashboard reverse-proxy (`/api/v0/*`, `/api/jobs/*`, `/logs/*`, `/ray-dashboard/*`, `/api/cluster_status`,
and the iframe's polling — all mounted by `ray.proxy_router`) generate steady INFO churn that says
nothing about app behavior. `LoggingMiddleware` returns early (no log line) when `request.url.path`
starts with one of a small `_NO_LOG_PREFIXES` tuple (`/_app`, plus the Ray proxy prefixes). Failures
on those paths still raise and surface via the exception handler; we just don't emit a success access
line for asset/proxy traffic.

```python
_NO_LOG_PREFIXES = ("/_app", "/api/v0", "/api/jobs", "/logs", "/ray-dashboard")

class LoggingMiddleware(BaseHTTPMiddleware):
    log = logging.getLogger("viewer.requests")
    async def dispatch(self, request, call_next):
        if request.url.path.startswith(_NO_LOG_PREFIXES):
            return await call_next(request)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self.log.exception("request_failed", extra={
                "method": request.method, "path": request.url.path,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1)})
            raise
        self.log.info("request_completed", extra={
            "method": request.method, "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - start) * 1000, 1)})
        return response
```

`request_id` is added by the filter, not duplicated here. (Keeping `TimingMiddleware` for the response
header is fine; the duration in the log is recomputed locally to avoid coupling the two middlewares.)

---

## 4. What to log, at which level, per layer

Level discipline (per observability skill): **DEBUG** dev diagnostics; **INFO** request lifecycle +
normal operations (request start/end, submissions, reconcile summary, orchestrator decisions);
**WARNING** recoverable anomalies (transient Ray errors we swallow, fallbacks); **ERROR** /
`log.exception` only for failures needing attention. Never log expected/handled conditions at ERROR.

| Layer / file | Event(s) | Level | Key `extra` fields |
|---|---|---|---|
| `viewer.requests` (LoggingMiddleware) | `request_completed`, `request_failed` | INFO / `exception` | method, path, status, duration_ms |
| `core/lifespan.py` | `startup_complete`, `shutdown_complete` (already clean) | INFO | — |
| `services/orchestrator/loop.py` | `orchestrator_loop_started`, `orchestrator_submitting`, `orchestrator_tick_failed`, `orchestrator_loop_stopping`, `ray_connected` | INFO / `exception` | pipeline, chunk_id, slot, interval_seconds |
| `services/orchestrator/derive.py` | `ray_summarize_failed`, `ray_get_job_info_failed` | WARNING | driver_job_id / submission_id, error |
| `services/sync.py` reconcile (via loop) | `s3_reconcile_complete` | INFO | cached_total, transcribed_total, rows_updated, by_status |
| `services/submission.py` | `chunk_submitted`; transient stop failure `stop_job_failed` | INFO / WARNING | chunk_id, chunk_total, pipeline, submission_id, batch_count |
| `services/ray_dashboard.py`, `services/discover/search.py` | keep existing warn/info/debug; restyle to event-name + extra | WARNING/INFO/DEBUG | url, table, error |
| `core/exceptions.py` | `domain_error` (5xx only, already `log.exception`) | `exception` | status, error_type (request_id auto via filter) |

The orchestrator's **submit / skip / cooldown** decisions are partly implicit today: `derive_state`
returns early when the state is not `ok` / a slot is `None`, and silently filters out in-flight +
cooled-down chunks. To make decisions observable **without unbounded payload growth**, add one INFO
summary per tick in `tick()` after `derive_state`, logging **counts only** (never the eligible
chunk-id lists — those are already named one-per-line by the per-submission `chunk_submitted` /
`orchestrator_submitting` lines, so repeating them in a per-60s tick line is redundant and grows
unboundedly):

```python
log.info("orchestrator_tick", extra={
    "prefetch_eligible_count": len(state.prefetch.eligible),
    "htr_eligible_count": len(state.htr.eligible),
    "cooldowns": len(state.cooldowns),
    "skipped_reason": None})  # set to "ray_unreachable" / "slot_unavailable" on the early-return paths
```

`skipped_reason` is the one genuinely-invisible thing today: the `if not state.ok: return` /
`if ray_client is None: return` / `if state.prefetch is None / state.htr is None: return` early-return
paths emit nothing. Logging one tick line with `skipped_reason` set on those paths (and `None` on the
submit path) makes them observable. Keep the per-submission `chunk_submitted` line; do not add a log
per skipped/cooled-down chunk (noise).

### Before / after examples

**A. Orchestrator submit — `loop.py:73`**

```python
# before
log.info(f"orchestrator: submitting {settings.prefetch_pipeline} for chunk {cid}")
# after
log.info("orchestrator_submitting", extra={"pipeline": settings.prefetch_pipeline, "chunk_id": cid, "slot": "prefetch"})
```

**B. Chunk submitted — `submission.py` (new line after submit succeeds, before `return SubmitResult(...)`)**

```python
# after (no equivalent before — submission was silent)
log.info("chunk_submitted", extra={
    "chunk_id": chunk_id, "chunk_total": membership.chunk_total,
    "pipeline": spec.name, "submission_id": submission_id,
    "batch_count": len(membership.batch_ids)})
```

**C. S3 reconcile summary — `loop.py:45` (capture the currently-discarded `SyncResult`)**

```python
# before
await reconcile_from_s3(session, hcp_endpoint=..., cache_bucket=..., output_bucket=...)
# after
result = await reconcile_from_s3(session, hcp_endpoint=..., cache_bucket=..., output_bucket=...)
log.info("s3_reconcile_complete", extra={
    "cached_total": result.cached_total, "transcribed_total": result.transcribed_total,
    "rows_updated": result.rows_updated, "by_status": result.by_status})
```

**D. Transient Ray warning — `derive.py:130`**

```python
# before
log.warning(f"ray tasks/summarize failed for job {driver_job_id}: {exc}")
# after
log.warning("ray_summarize_failed", extra={"driver_job_id": driver_job_id, "error": str(exc)})
```

**E. Tick failure — `loop.py:106` (keep traceback)**

```python
# before
log.exception("orchestrator tick failed; continuing")
# after — already event-name-shaped; rename for consistency, exc_info via .exception()
log.exception("orchestrator_tick_failed")
```

---

## 5. Out of scope (explicitly)

- **OpenTelemetry**: no SDK init, no OTLP exporter, no `LoggingInstrumentor`, no traces/metrics, no
  span creation. Only the *record shape* is OTel-ready. Wiring OTel is a separate change.
- **Log shipping / aggregation**: no file handlers, rotation, Loki/ELK/Fluent Bit, no collector. We
  log JSON to stdout; whatever runs the process captures it.
- **No `structlog`, no `python-json-logger`, no Prometheus client, no ad-hoc tracing.**
- **No second formatter / no `RASK_LOG_FORMAT`.** One `JsonFormatter`. Human-readable dev output is
  deferred until the dev-loop friction is actually felt.
- **No per-request DEBUG body/header dumps**, no metric labels carrying request_id/paths (high
  cardinality belongs in logs, not metrics — relevant only once OTel metrics exist).
- **uvicorn `--log-config` file**: not needed; uvicorn loggers are folded into our `dictConfig` from
  `configure_logging`. The `Makefile` serve line stays unchanged.

---

## Change list (files + edits)

| File | Edit |
|---|---|
| `core/context.py` | **NEW** — `request_id_ctx: ContextVar[str]` + `current_request_id()`. |
| `core/logging.py` | **NEW** — `RequestIdFilter`, `JsonFormatter` (fixed `_EXTRA_KEYS` allowlist, no `__dict__` diff), `configure_logging(settings)` building one `dictConfig` (single JSON console handler + filter; root at `log_level`; `uvicorn`/`uvicorn.error` folded in with `propagate=False`; `uvicorn.access` silenced). No import-time effects. |
| `core/config.py` | Add `log_level: Literal["DEBUG","INFO","WARNING","ERROR"]` (`RASK_LOG_LEVEL`, default `INFO`). |
| `core/lifespan.py` | In `make_lifespan`'s `lifespan`, call `configure_logging(settings)` as the first statement (before `app.state` setup and before `startup_complete`). |
| `core/middleware.py` | `RequestIDMiddleware.dispatch`: set `request_id_ctx` with token, `reset(token)` in `finally`. Add `LoggingMiddleware` (logger `viewer.requests`, `request_completed` / `request_failed` with method/path/status/duration_ms; skip `_NO_LOG_PREFIXES`). Register it last in `register_middleware` (order: CORS → RequestID → Timing → Logging). Update module docstring (drop "logging deferred" note). |
| `services/orchestrator/loop.py` | Restyle `log.*` to event-name + `extra`; capture `SyncResult` → `s3_reconcile_complete`; add one `orchestrator_tick` decision summary (counts + `skipped_reason`, no id lists); rename `orchestrator_tick_failed`. |
| `services/submission.py` | Add `chunk_submitted` INFO after submit; restyle `stop_job_failed` warning to event-name + extra. |
| `services/orchestrator/derive.py` | Restyle the two transient `log.warning` calls (`ray_summarize_failed`, `ray_get_job_info_failed`) to event-name + `extra`. |
| `services/ray_dashboard.py`, `services/discover/search.py`, `core/lifespan.py` | Restyle remaining f-string log calls to event-name + `extra` (lifespan's `startup_complete`/`shutdown_complete` already conform; `ray_dashboard.py` has `Ray dashboard unreachable` info + a `per-node detail unavailable` debug; `search.py` has the `thumb GET … failed` warning). |

> `services/discover/catalog.py` has a `log = logging.getLogger(__name__)` getter but **zero**
> `log.*` call sites — nothing to restyle there, so it is intentionally omitted from this table.

No new dependency is added to `pyproject.toml`.

## How OTel slots in later (no code change to logs)

When OTel is adopted: install the OTel SDK + `opentelemetry-instrumentation-logging`, set
`OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true` (or call `LoggingInstrumentor().instrument()`
in the SDK bootstrap), and configure the OTLP log exporter. The instrumentor attaches an OTel
`LoggingHandler` to the root logger. Because every record is already a **static event name + `extra`
attributes** with `request_id` stamped by the filter, those records forward as OTel log records and
their `extra` keys become log-record/span attributes — carrying trace/span IDs automatically — with
**no edit to any log call**. Our `JsonFormatter` console handler can remain as a second sink or be
dropped; the logging *call sites* are final after this change.
