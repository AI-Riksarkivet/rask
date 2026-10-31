# Commit-turn waiting: what a waiter should hold, and for how long

> Findings for ADR 0186 (the commit turn, `packages/service-kit/src/service_kit/lakehouse/commit_turns.py`). This
> note answers one question: while a writer waits on a held turn, what resource does it hold, and how do comparable
> systems bound that? Every claim cites a primary source (source at a named commit or tag, or an official doc page
> at a named version), read 2026-10-31. Claims I could not confirm are marked **unconfirmed** with where I looked.
> Paragraphs labelled **Inference** are this note's reasoning, not a source's.

The measured baseline (stated by the brief, not re-measured here): `CommitTurns.wait`
(`commit_turns.py:153-174`) polls the record with `time.sleep` (20 ms first, 250 ms cap, `commit_turns.py:73-74`)
for up to `HOLD` = 30 s (`commit_turns.py:63`) inside a threadpool thread; with 50 such waiters a describe of an
unrelated table took 26.8 s, with 20 it took 0.02 s.

Sources pinned:

| Source | Ref |
| --- | --- |
| anyio | tag `4.14.2` (`c384f99`), the version in rask's `uv.lock` |
| starlette | tag `1.3.1` (`8ebffd0`), the version in rask's `uv.lock` |
| fastapi | tag `0.140.13` (`628663f`), the version in rask's `uv.lock` (uvicorn `0.51.0`) |
| lakekeeper | `main` at `5592a7b389f021cd6ae27418f18dbbf3d588a0a3` (2026-10-31) |
| polaris | `main` at `b1e9451d3cccaaa654ff08181b15525089085201` (2026-10-30) |
| iceberg | `main` at `be2528185a1d37e6590bce193c2760202d90a0cb` (2026-10-30) |
| hudi | `master` at `c3cbf4ea6ce2a99d52a8c55ea76b13b29e807dea` (2026-10-31) |
| unitycatalog | `main` at `06aeea8a9e242c73ff4b8519d5a9c77f210d41e4` |
| delta | `master` at `719768686369f1d52e6891aec1f7fa1be903975c` |
| resilience4j | `master` at `aa9aacb8cb5333f2dee2590ff07f05f2c942d602` (2026-10-30) |
| Polly | `main` at `289f29808f70f49d3d46de57f42fcd1139bc2bd0` (2026-10-30) |
| client-go | `master` at `b51563e839b761a4b85dca15faf4a9bac49a84c0` (2026-10-31) |
| etcd | `main` at `1573d59416efa220421c451f5266b24a5d8374ef`; docs from `etcd-io/website` at `0c82d27` (v3.6 docs) |
| zookeeper | `master` at `858c8d770a1ef0952b4de98d8c35b4e2b52e83c0` |

## 1. anyio, Starlette, FastAPI: the thread limiter and long waits in sync handlers

**The limiter and its default.** A sync (`def`) path operation runs through Starlette's `run_in_threadpool`, which
is `await anyio.to_thread.run_sync(func)` with no `limiter` argument
([starlette/concurrency.py:32-34 @1.3.1](https://github.com/encode/starlette/blob/8ebffd0678570ddd5d5bb11c6f3c3c7fd4682ab9/starlette/concurrency.py#L32-L34));
FastAPI routes a sync endpoint to it
([fastapi/routing.py:129 @0.140.13](https://github.com/fastapi/fastapi/blob/628663f4f899c465da423bce681c7adf9a218948/fastapi/routing.py#L129))
and does the same for sync dependencies
([fastapi/dependencies/utils.py:676](https://github.com/fastapi/fastapi/blob/628663f4f899c465da423bce681c7adf9a218948/fastapi/dependencies/utils.py#L676)).
anyio's asyncio backend creates the default limiter lazily, once per event loop (a `RunVar`), as `CapacityLimiter(40)`
([anyio/_backends/_asyncio.py:2166, 3093-3099 @4.14.2](https://github.com/agronholm/anyio/blob/c384f99687c64c59ed8a11c3a0f11a2d57daff71/src/anyio/_backends/_asyncio.py#L3093-L3099)),
and `run_sync_in_worker_thread` holds a token for the whole time the function runs: `async with limiter or
cls.current_default_thread_limiter():` wraps the dispatch and the wait for the result (`_asyncio.py:2603`). A token
is therefore held by a thread that is `time.sleep`ing exactly as by one doing IO.

- anyio docs, *Adjusting the default maximum worker thread count*: "The default AnyIO worker thread limiter has a
  value of **40** ... You can adjust this limit like this: `to_thread.current_default_thread_limiter().total_tokens = 60`"
  ([docs/threads.rst:281-295 @4.14.2](https://github.com/agronholm/anyio/blob/c384f99687c64c59ed8a11c3a0f11a2d57daff71/docs/threads.rst#L281-L295)).
  Because the limiter is per event loop, the assignment must run inside the running loop (for an app, in the
  lifespan); `current_default_thread_limiter()` raises outside one
  ([src/anyio/to_thread.py:70-80](https://github.com/agronholm/anyio/blob/c384f99687c64c59ed8a11c3a0f11a2d57daff71/src/anyio/to_thread.py#L70-L80)).
- `to_thread.run_sync` takes an explicit `limiter`: "capacity limiter to use to limit the total amount of threads
  running (if omitted, the default limiter is used)" (`to_thread.py:49-50`). A caller can run a class of work under
  its own `CapacityLimiter` and leave the default untouched.
- Cancellation: "By default, tasks are shielded from cancellation while they are waiting for a worker thread to
  finish. ... `abandon_on_cancel=True` ... Note, however, that the thread will still continue running"
  ([docs/threads.rst:25-28](https://github.com/agronholm/anyio/blob/c384f99687c64c59ed8a11c3a0f11a2d57daff71/docs/threads.rst#L25-L28));
  the shield is `CancelScope(shield=not abandon_on_cancel)` (`_asyncio.py:2604`). Code in a worker thread can only
  cooperate: "call `from_thread.check_cancelled`" (docs/threads.rst:297-316). A sleeping waiter whose client went
  away keeps its token until its own deadline.
- Starlette docs, *Thread Pool / Concurrency Limitations*: "The default thread pool size is only 40 _tokens_. This
  means that only 40 threads can run at the same time. This limit is shared with other libraries: for example FastAPI
  also uses `anyio` to run sync dependencies ... If you need to run more threads, you can increase the number of
  _tokens_ ... `limiter.total_tokens = 100` ... Increasing the number of threads may have a performance and memory
  impact, so be careful when doing so."
  ([docs/threadpool.md:16-32 @1.3.1](https://github.com/encode/starlette/blob/8ebffd0678570ddd5d5bb11c6f3c3c7fd4682ab9/docs/threadpool.md#L16-L32)).
- FastAPI docs, *Path operation functions*: "When you declare a *path operation function* with normal `def` instead
  of `async def`, it is run in an external threadpool that is then awaited" and dependencies likewise
  ([docs/en/docs/async.md:418, 426 @0.140.13](https://github.com/fastapi/fastapi/blob/628663f4f899c465da423bce681c7adf9a218948/docs/en/docs/async.md#L418));
  the TL;DR says use `async def` when the library is awaited and `def` when it "doesn't have support for using
  `await`" (async.md:7-40). Neither FastAPI's nor Starlette's docs discuss long deliberate waits (sleeps, lock polls)
  in a sync handler; **unconfirmed** that either documents "wait in the event loop with `asyncio.sleep` between short
  threadpool reads" as an idiom. Looked in: starlette `docs/*.md`, fastapi `docs/en/docs` (grep for threadpool,
  thread pool, total_tokens, sleep). The idiom follows from what they do document: the token is held only while a
  function runs in a thread (above), and `async def` is for code whose waits are awaited.
- The project's FastAPI references
  (`~/.claude/plugins/cache/ra-skills/fastapi/d523325bbfa6/references/`):
  - `core-conventions.md` § Async vs sync path operations (lines 271-295): "Use `async def` only when the body is
    genuinely async-compatible (every blocking call is `await`ed or non-blocking). Otherwise use plain `def` — FastAPI
    runs sync functions in a threadpool so they don't block the event loop." and "**Never run blocking code inside an
    `async def`** — it works but kills throughput. For mixing blocking and async, see the Asyncer pattern".
  - `production-patterns.md` § Bridging sync ↔ async with Asyncer (lines 171-192): "When you must call blocking code
    from an async handler, **don't** import `anyio.to_thread.run_sync` directly — use `asyncer.asyncify()`. Same
    primitive" and "it integrates with Starlette's existing threadpool budget (default 40 workers — saturating it slows
    every sync route)." § Graceful shutdown (line 169) points a route "slower than the grace period" at a background
    worker. Neither file speaks to raising the limiter or to a dedicated limiter.

**Inference (what this implies for the commit turn).** The 26.8 s describe is the documented behaviour: 50 waiters
each hold one of 40 default tokens for up to 30 s while sleeping, and every other sync door queues behind them. Three
levers exist, and only one removes the cause. (a) Raising `total_tokens` moves the cliff (Starlette warns of memory
and performance cost) and needs a value set in the lifespan from configuration; it bounds nothing. (b) A dedicated
`CapacityLimiter` for waiting (`to_thread.run_sync(..., limiter=...)`) is a bulkhead (§3): it protects the other
doors, but a waiter still holds a thread. (c) Doing the wait in the event loop, `await anyio.sleep(backoff)` between
short `to_thread.run_sync(self._read)` calls, holds a token only for each read (milliseconds) and makes the wait
cancellable (a vanished client stops waiting). This matches the reference's rule that awaited waits belong in
`async def`. The ADR's callers are partly `async def` already (`versions.py:191 create_table_version`), so (c)
fits where the door is async and needs the wait split out of the sync path where it is not. (b) and (c) combine: the
read calls can run under a small dedicated limiter, so waiters cannot exhaust the default pool even in bulk. All
three meet the four facts the same way (in-process, no state outside git); (c) is the only one that also makes a
waiter's cost independent of its count, which is the property the measurement says is missing.

## 2. How similar catalogs bound what a waiting request holds

### Lakekeeper

- **Table commits are optimistic, retried in the server, and the retry wait is async.** `MAX_RETRIES_ON_CONCURRENT_UPDATE: usize = 2`
  ([crates/lakekeeper/src/server/tables.rs:109](https://github.com/lakekeeper/lakekeeper/blob/5592a7b389f021cd6ae27418f18dbbf3d588a0a3/crates/lakekeeper/src/server/tables.rs#L109)).
  On a `CONCURRENT_UPDATE_ERROR_TYPE` the loop backs off "Short jittered exponential backoff ... First delay: 50ms,
  then 100ms, 200ms, ..., up to 3200ms" with `tokio::time::sleep(...).await` (tables.rs:1420-1440), so a waiting
  commit holds a tokio task and no thread. The transaction is opened inside `try_commit_tables` per attempt, so the
  back-off sleep holds no pooled connection (inferred from the loop shape at tables.rs:1395-1404; I did not trace
  `try_commit_tables` to confirm the transaction is closed before return, **unconfirmed**).
- **View commits take a pessimistic row lock**: `SELECT metadata_location FROM tabular ... FOR UPDATE`, commented
  "This is a deliberate pessimistic lock, held through the sub-metadata writes below"
  ([crates/lakekeeper-storage-postgres/src/tabular/view/mod.rs:110-127](https://github.com/lakekeeper/lakekeeper/blob/5592a7b389f021cd6ae27418f18dbbf3d588a0a3/crates/lakekeeper-storage-postgres/src/tabular/view/mod.rs#L110-L127)).
  A view commit waiting on that lock holds a write-pool connection inside an open transaction. No `lock_timeout` is
  set in the view module (grep of `tabular/view/*.rs` for `lock_timeout|statement_timeout` returns nothing), so the
  wait is bounded only by Postgres, whose `lock_timeout` "A value of zero (the default) disables the timeout"
  ([PostgreSQL 18 docs, runtime-config-client, lock_timeout](https://www.postgresql.org/docs/18/runtime-config-client.html#GUC-LOCK-TIMEOUT)).
  The view retry loop reuses `MAX_RETRIES_ON_CONCURRENT_UPDATE` (crates/lakekeeper/src/server/views/commit.rs:167-171).
- **Where Lakekeeper does bound a lock wait, it says why.** The grant apply sets `SET LOCAL lock_timeout = '3s'`
  (crates/lakekeeper-storage-postgres/src/grant.rs:221) and documents: "`lock_timeout` bounds the wait so a stuck
  holder fails fast with a typed retriable error instead of hanging ... Unbounded, the apply would wait behind them
  while holding the advisory lock ... and keeping a write-pool connection past the request's own timeout"
  (grant.rs:193-203); a `55P03` lock timeout or `40P01` deadlock is mapped to a retriable conflict "rather than a 503
  that reads as 'backend down'" (grant.rs:126-136).
- **Pool size and acquire timeout.** Defaults `pg_read_pool_connections: 10`, `pg_write_pool_connections: 5`,
  `pg_acquire_timeout: 5` seconds
  ([crates/lakekeeper-storage-postgres/src/config.rs:56-58](https://github.com/lakekeeper/lakekeeper/blob/5592a7b389f021cd6ae27418f18dbbf3d588a0a3/crates/lakekeeper-storage-postgres/src/config.rs#L56-L58)),
  applied as `.acquire_timeout(Duration::from_secs(self.pg_acquire_timeout))` (lib.rs:311). The configuration doc
  table lists `LAKEKEEPER__PG_ACQUIRE_TIMEOUT` default `5` and the pool sizes without a default column value
  ([docs/docs/configuration.md:68-81](https://github.com/lakekeeper/lakekeeper/blob/5592a7b389f021cd6ae27418f18dbbf3d588a0a3/docs/docs/configuration.md#L68-L81));
  task-workers.md:77 states "default 10 plus 5".
- **Pool exhausted.** `begin_write`/`begin_read` count `sqlx::Error::PoolTimedOut` as an acquire timeout
  (lib.rs:94-115; pool_metrics.rs:57) and turn it into `ErrorModel::internal(..., "DatabaseError", ...)`
  (dbutils.rs:68-69), i.e. the request fails after 5 s with an internal error rather than waiting on. The monitoring
  doc names `lakekeeper_catalog_pg_pool_acquire_timeouts_total` "direct evidence of pool exhaustion" and says "Any
  nonzero rate ... means requests are already being delayed or failing — alert on it"
  ([docs/docs/monitoring.md:298-310](https://github.com/lakekeeper/lakekeeper/blob/5592a7b389f021cd6ae27418f18dbbf3d588a0a3/docs/docs/monitoring.md#L298-L310)).
  The HTTP status `ErrorModel::internal` maps to (500) is from the type's name; I did not open its definition,
  **unconfirmed**.

### Apache Polaris (NoSQL persistence)

- **The per-reference lock is an in-JVM `Semaphore(1)`**, keyed by `(realmId, refName)` in a static
  `ConcurrentHashMap`; `before(nanosRemaining)` is `semaphore.tryAcquire(nanosRemaining, NANOSECONDS)`
  ([persistence/nosql/persistence/impl/.../commits/ExclusiveCommitSynchronizer.java:26-52](https://github.com/apache/polaris/blob/b1e9451d3cccaaa654ff08181b15525089085201/persistence/nosql/persistence/impl/src/main/java/org/apache/polaris/persistence/nosql/impl/commits/ExclusiveCommitSynchronizer.java#L26-L52)).
  It serialises commits within one process only; across replicas the commit is still a CAS retried by the loop.
- **The 15 s is the whole retry loop's budget, not only the semaphore's.** `RetryConfig`: `DEFAULT_TIMEOUT = "PT15S"`,
  `DEFAULT_RETRIES = "10000"`, initial sleep 10-20 ms doubling, `DEFAULT_RETRY_MAX_SLEEP = "PT0.250S"`, fair retries
  `SLEEPING` by default
  ([persistence/nosql/persistence/api/.../commit/RetryConfig.java:29-75](https://github.com/apache/polaris/blob/b1e9451d3cccaaa654ff08181b15525089085201/persistence/nosql/persistence/api/src/main/java/org/apache/polaris/persistence/nosql/api/commit/RetryConfig.java#L29-L75)).
  `CommitterImpl.commit` passes the loop's remaining nanos to `sync.before(...)`; a failed acquire returns
  `Optional.empty()`, which the loop treats as a retry (CommitterImpl.java:107-133). When time runs out the loop
  throws `RetryTimeoutException` (RetryLoopImpl.java:104-111), which `commitRuntimeException` rethrows as a
  `RuntimeException` (Committer.java:80-88). The metastore commits use `.synchronizingLocally()`
  (NoSqlMetaStore.java:165, 480). The HTTP status a timed-out commit reaches the client as: **unconfirmed** (did not
  trace the exception mapper).
- **What a waiter holds: the calling thread.** Both the semaphore wait and the back-off (`monotonicClock.sleepMillis`,
  RetryLoopImpl.java:133-145) block the thread. Polaris's REST resources are synchronous JAX-RS methods on Quarkus
  REST, where "Methods that return results directly are treated as blocking and run on a worker thread"
  ([Quarkus REST guide, Execution model, blocking, non-blocking, 3.40](https://quarkus.io/guides/rest)); the worker
  pool's `quarkus.thread-pool.max-threads` is "the greater of 8 times the number of available processors and 200"
  and `quarkus.thread-pool.queue-size` "For most applications this should be unbounded"
  ([Quarkus all-config, 3.40](https://quarkus.io/guides/all-config)). Polaris sets neither in
  `runtime/defaults/src/main/resources/application.properties` (grep for `thread`), and I found no
  `@Blocking`/`@NonBlocking`/`@RunOnVirtualThread` in `runtime/` main code. That Polaris's generated REST interfaces
  return non-reactive types is my reading, **unconfirmed** against the generated sources.
- **Bound on concurrent waiters: none found.** No count limit on semaphore waiters; the realm rate limiter defaults to
  `polaris.rate-limiter.filter.type=no-op` (application.properties:227-229).
- **Server-side priority for a writer that has been retrying.** `SleepingFairRetries` keeps 16 per-JVM counters by
  retry count; before each attempt a loop sleeps `highestOneBit(number of loops at its retry count or higher)` ms, so
  fresh attempts yield to ones that have retried more (SleepingFairRetries.java:23-58). The doc comment: "very
  frequently started retry-loops running against highly contended resources can result in some retry-loops
  invocations never making any progress ... The default 'fair retries type' helps in these scenarios" (RetryConfig.java:60-69).
  The priority is per process and keyed by nothing but the loop's own attempt count.

### Apache Iceberg

- **LockManager defaults** ([core/.../CatalogProperties.java:138-153](https://github.com/apache/iceberg/blob/be2528185a1d37e6590bce193c2760202d90a0cb/core/src/main/java/org/apache/iceberg/CatalogProperties.java#L138-L153)):
  `lock.heartbeat-interval-ms` 3 s, `lock.heartbeat-timeout-ms` 15 s, `lock.heartbeat-threads` 4,
  `lock.acquire-interval-ms` 5 s, `lock.acquire-timeout-ms` 3 min; the docs table lists the same
  ([docs/docs/catalog-properties.md:60-62](https://github.com/apache/iceberg/blob/be2528185a1d37e6590bce193c2760202d90a0cb/docs/docs/catalog-properties.md#L60-L62)).
  The in-memory manager acquires by `Tasks.foreach(entityId).retry(Integer.MAX_VALUE - 1)...exponentialBackoff(acquireIntervalMs, acquireIntervalMs, acquireTimeoutMs, 1)`
  (core/.../util/LockManagers.java:239-251): a blocking poll every 5 s for up to 3 min, on the calling thread.
- **Where the wait runs: in the process that owns the catalog implementation**, which for Hadoop/Glue/DynamoDB
  catalogs is the writer's (client's) process: `HadoopTableOperations` calls `lockManager.acquire(...)` around its
  rename commit (core/.../hadoop/HadoopTableOperations.java:363, 397). A REST catalog takes no LockManager in the
  client; how its server serialises is the server's.
- **REST reference handlers retry the commit server-side** with the table defaults:
  `retry(COMMIT_NUM_RETRIES_DEFAULT)` (4), `exponentialBackoff(100 ms, 60 s, 30 min, 2.0)`, only on
  `CommitFailedException`, refreshing the base each retry
  ([core/.../rest/CatalogHandlers.java:619-633](https://github.com/apache/iceberg/blob/be2528185a1d37e6590bce193c2760202d90a0cb/core/src/main/java/org/apache/iceberg/rest/CatalogHandlers.java#L619-L633);
  defaults at core/.../TableProperties.java:89-99). The back-off is a blocking sleep on the request thread
  (`Tasks` is Iceberg's synchronous retry utility; that it sleeps the calling thread is my reading of its use here,
  **unconfirmed** in `util/Tasks.java`, not opened).

### Apache Hudi

- **Defaults** ([hudi-common/.../config/LockConfiguration.java:32-49](https://github.com/apache/hudi/blob/c3cbf4ea6ce2a99d52a8c55ea76b13b29e807dea/hudi-common/src/main/java/org/apache/hudi/common/config/LockConfiguration.java#L32-L49);
  [hudi-client-common/.../config/HoodieLockConfig.java:79-125](https://github.com/apache/hudi/blob/c3cbf4ea6ce2a99d52a8c55ea76b13b29e807dea/hudi-client/hudi-client-common/src/main/java/org/apache/hudi/config/HoodieLockConfig.java#L79-L125)):
  `hoodie.write.lock.wait_time_ms` 60 000 ("Timeout in ms, to wait on an individual lock acquire() call, at the lock
  provider"), `wait_time_ms_between_retry` 1000, `max_wait_time_ms_between_retry` 16 000, `num_retries` 15 (at
  each lock provider), `client.wait_time_ms_between_retry` 5000, `client.num_retries` 50 (lock manager),
  `heartbeat_interval_ms` 60 000.
- **Where it runs: in the writer.** `LockManager.lock()` wraps `getLockProvider().tryLock(writeConfig.getLockAcquireWaitTimeoutInMs(), ...)`
  in a `RetryHelper` (client/transaction/lock/LockManager.java:66-90), inside the write client. There is no catalog
  server in the path.
- **Its object-storage lease** (the closest analogue to rask's turn record): `StorageBasedLockProvider.tryLock(time)`
  loops `tryLock()` then `Thread.sleep(1000)` until the deadline (StorageBasedLockProvider.java:232-250); the lease's
  `validity.timeout.secs` defaults to 5 min and `renew.interval.secs` to 30 s, and the config refuses a validity less
  than 10x the renew interval (StorageBasedLockConfig.java:34-56, 84-97). The holder heartbeats; the waiter polls.

### Delta Lake coordinated commits and Unity Catalog

- Delta's `CommitCoordinatorClient.commit(...)` is a single call that either ratifies `commitVersion` or throws
  `CommitFailedException` (storage/.../commit/CommitCoordinatorClient.java:78-99), which carries `retryable` and
  `conflict` flags ("yes/yes: physical conflict (allowed to rebase and retry)", CommitFailedException.java:31-47). The
  retry is the client's.
- Unity Catalog serialises commits on a table row lock: `lockTableForCommit` takes `PESSIMISTIC_WRITE`, and "A
  lock-wait timeout or deadlock victim is reported as `lockContentionError`": Delta gets `COMMIT_STATE_UNKNOWN`
  (500, so the client resends the identical, UUID-deduplicated commit), Iceberg gets 409, table deletion 503
  ([server/.../persist/utils/RepositoryUtils.java:32-101](https://github.com/unitycatalog/unitycatalog/blob/06aeea8a9e242c73ff4b8519d5a9c77f210d41e4/server/src/main/java/io/unitycatalog/server/persist/utils/RepositoryUtils.java#L32-L101)).
  UC caps unbackfilled commits at `MAX_NUM_COMMITS_PER_TABLE = 10` (DeltaCommitRepository.java:138-147).
- **What a UC waiter holds**: a blocking-executor thread and a pooled DB connection, and UC sizes the first by the
  second: `BlockingTaskExecutor.builder().numThreads(hibernateConfigurator.getDataSource().getMaximumPoolSize())`
  (server/.../UnityCatalogServer.java:121-122), with annotated services on `useBlockingTaskExecutor(true)`
  (ArmeriaServerBuilder.java:392). The lock-wait bound itself: I found no `lock.timeout`/`lock_timeout` setting in
  `server/src/main` (grep), so it rests on the database default, **unconfirmed** for MySQL/H2.
- **Server-side priority for a slow writer**: none found in either. Looked in: Delta `storage/.../commit`,
  `spark/.../coordinatedcommits` file list; UC `DeltaCommitRepository.java`, `RepositoryUtils.java`. The only
  priority mechanism found in any catalog read here is Polaris's per-JVM `SleepingFairRetries`.

**Inference (what this implies for the commit turn).** No catalog read here lets a waiter hold a scarce resource
unbounded and unnoticed: Lakekeeper waits on a tokio timer (holding nothing scarce) and fails an exhausted pool in
5 s with a counted metric; UC caps waiters at the connection pool size by construction; Lakekeeper's grant path sets
`lock_timeout` precisely so a waiter does not keep a write-pool connection "past the request's own timeout". Polaris
blocks a Quarkus worker thread for up to 15 s but has a pool of at least 200. rask's waiter blocks one of 40 shared
tokens for up to 30 s, the longest wait on the smallest pool in this set. rask's server-side preference for a
retrying writer (the handed turn) has one precedent, Polaris's fair retries, and that is per process and keyed by
attempt count, not by caller. The Lance-idiomatic point holds: like Lakekeeper's table path and Polaris across
replicas, the commit itself stays the store's CAS (Lance's own commit), and the turn is an advisory ordering on top;
a waiter that gives up loses ordering, never correctness.

## 3. The bulkhead pattern: reject, queue, or let through?

- **resilience4j `SemaphoreBulkhead`**: `DEFAULT_MAX_CONCURRENT_CALLS = 25`, `DEFAULT_MAX_WAIT_DURATION = Duration.ofSeconds(0)`,
  fair call handling on by default
  ([resilience4j-bulkhead/.../BulkheadConfig.java:33-36](https://github.com/resilience4j/resilience4j/blob/aa9aacb8cb5333f2dee2590ff07f05f2c942d602/resilience4j-bulkhead/src/main/java/io/github/resilience4j/bulkhead/BulkheadConfig.java#L33-L36)).
  `maxWaitDuration`: "a maximum amount of time which the calling thread will wait to enter the bulkhead. If bulkhead
  has space available, entry is guaranteed and immediate. If bulkhead is full, calling threads will contest for
  space, if it becomes available. maxWaitDuration can be set to 0." and "for threads running on an event-loop ...
  blocking the thread must be avoided ... should set maxWaitDuration to 0" (BulkheadConfig.java:140-157). When no
  permit arrives in time, `acquirePermission()` throws `BulkheadFullException`
  (internal/SemaphoreBulkhead.java:171-180). **Full means rejected** (immediately by default, after
  `maxWaitDuration` otherwise); a call is never let through unprotected.
- **resilience4j `ThreadPoolBulkhead`**: `DEFAULT_QUEUE_CAPACITY = 100`, core pool `availableProcessors - 1` (min 1),
  max pool `availableProcessors` (ThreadPoolBulkheadConfig.java:43-50); when the pool and queue are full the executor's
  `RejectedExecutionException` becomes `BulkheadFullException` (internal/FixedThreadPoolBulkhead.java:47, 160-165).
  **Bounded queue, then reject.**
- **Polly**: v8 replaces the v7 bulkhead with `ConcurrencyLimiter`: "The new counterpart to bulkhead is
  `ConcurrencyLimiter`" ([docs/migration-v8.md:499-504](https://github.com/App-vNext/Polly/blob/289f29808f70f49d3d46de57f42fcd1139bc2bd0/docs/migration-v8.md#L499-L504));
  v7's `Policy.Bulkhead(maxParallelization, maxQueuingActions)` maps to `AddConcurrencyLimiter(permitLimit, queueLimit)`
  (migration-v8.md:506-545). Defaults `DefaultPermitLimit = 1000`, `DefaultQueueLimit = 0`
  (src/Polly.RateLimiting/RateLimiterConstants.cs:7-9; docs/strategies/rate-limiter.md:140, "`PermitLimit` set to
  1000 and `QueueLimit` set to 0"). Over the limit the strategy throws `RateLimiterRejectedException`, which may carry
  `RetryAfter` (rate-limiter.md:11, 119-131). **Bounded queue (default zero), then reject.** v7's
  `BulkheadRejectedException` name is from memory, **unconfirmed** (v7 sources not opened).

**Inference (what this implies for the commit turn).** The established semantics are uniform: a bulkhead caps
concurrent work, optionally queues a bounded number more, and rejects the rest with a typed, retriable error; none
lets an overflow call through unprotected. Mapped onto the turn: a dedicated `CapacityLimiter` for turn waits (§1b)
is a bulkhead whose overflow choice must be stated. Rejecting (a typed retriable conflict, the catalog's existing
`CommitTurnBusyError` path) keeps Lance's own commit as the arbiter: the client's commit retry handles it, as UC's 409
for Iceberg relies on the client's retry. Letting the overflow skip the wait and go straight to the CAS is the third
option the pattern does not have; it is still correct here (the CAS decides) and costs only ordering, but it is not a
bulkhead and should be named as "the turn is advisory past N waiters" if chosen. All of these are in-process,
declared from code and chart values (GitOps), and leave no live-only state.

## 4. Lease holders that come back versus ones that vanished

- **Kubernetes `coordination.k8s.io/v1` Lease**: `holderIdentity`; `leaseDurationSeconds` "a duration that
  candidates for a lease need to wait to force acquire it. This is measured against the time of last observed
  renewTime"; `renewTime` "when the current holder of a lease has last updated the lease"; `leaseTransitions`;
  `preferredHolder` "signals to a lease holder that the lease has a more optimal holder and should be given up"
  ([client-go applyconfigurations/coordination/v1/leasespec.go:29-54](https://github.com/kubernetes/client-go/blob/b51563e839b761a4b85dca15faf4a9bac49a84c0/applyconfigurations/coordination/v1/leasespec.go#L29-L54)).
- **client-go leaderelection**: "Core clients default" `LeaseDuration` 15 s, `RenewDeadline` 10 s, `RetryPeriod` 2 s;
  "A client needs to wait a full LeaseDuration without observing a change to the record before it can attempt to take
  over"
  ([tools/leaderelection/leaderelection.go:137-160](https://github.com/kubernetes/client-go/blob/b51563e839b761a4b85dca15faf4a9bac49a84c0/tools/leaderelection/leaderelection.go#L137-L160)).
  A non-renewing holder is detected on the **observer's own clock**: the elector records `observedTime` when the raw
  record changes, and the lease has expired when `obsTime + LeaseDurationSeconds` is before now (leaderelection.go:690-705);
  the record's `renewTime` is not compared across clocks, which is why the doc discusses skew *rate*, not skew
  (leaderelection.go:28-45). Acquire attempts run under `wait.JitterUntilWithContext(..., RetryPeriod, JitterFactor, true)`
  with `JitterFactor = 1.2` (leaderelection.go:74, 355-377). Validation requires `LeaseDuration > RenewDeadline > JitterFactor*RetryPeriod`
  (leaderelection.go:83-96).
- **What happens to a holder that lapsed**: without `Recovery`, "Run returns once renewal has failed for RenewDeadline
  and the caller is expected to exit"; with the alpha `Recovery`, the elector keeps renewing "the lease it held" and
  write requests through `WriteGate` are rejected meanwhile, and "Recovery only ever renews the lease this elector
  still holds, it never re-acquires it" (leaderelection.go:186-215). The consequence of lapsing is loss of the
  lease plus the full `LeaseDuration` wait every other candidate also pays; there is **no identity-keyed penalty**
  (no back-off or cool-down keyed by `holderIdentity`) in client-go. Looked in: `leaderelection.go` in full for
  backoff/penalty logic.
- **etcd leases**: "Leases are a mechanism for detecting client liveness. The cluster grants leases with a
  time-to-live. A lease expires if the etcd cluster does not receive a keepAlive within a given TTL period ... When a
  lease expires or is revoked, all keys attached to that lease will be deleted"
  ([website content/en/docs/v3.6/learning/api.md:417-421](https://github.com/etcd-io/website/blob/0c82d2741684f33834075552a0106de7d9346764/content/en/docs/v3.6/learning/api.md#L417-L421));
  refresh is a bidirectional `LeaseKeepAlive` stream (api.md:460-481). The concurrency `Session` defaults to
  `defaultSessionTTL = 60` s and keeps the lease alive in a goroutine
  ([client/v3/concurrency/session.go:26-80](https://github.com/etcd-io/etcd/blob/1573d59416efa220421c451f5266b24a5d8374ef/client/v3/concurrency/session.go#L26-L80)).
  A `Mutex.Lock` waiter does not poll: it waits on a watch for the deletion of keys with lower create revisions, so
  waiters queue in arrival order (client/v3/concurrency/mutex.go:75-95). The lock service: "The lock is held until
  Unlock is called on the key or the lease associate with the owner expires ... The next Lock caller waiting for the
  lock will then be woken up" (website dev-guide/api_concurrency_reference_v3.md:13-21). Expiry is the server's,
  keyed by lease ID; no penalty for a lapsed lease beyond losing it.
- **ZooKeeper sessions**: the negotiated timeout must be "a minimum of 2 times the tickTime ... and a maximum of 20
  times the tickTime"; "Session expiration is managed by the ZooKeeper cluster itself, not by the client ...
  Expirations happens when the cluster does not hear from the client within the specified session timeout period
  (i.e. no heartbeat). At session expiration the cluster will delete any/all ephemeral nodes owned by that session";
  a client that reconnects after expiry is told "expired" and must create a new session ("Only create a new session
  when you are notified of session expiration (mandatory)")
  ([zookeeper-website/.../programmers-guide/sessions.mdx:59-112](https://github.com/apache/zookeeper/blob/858c8d770a1ef0952b4de98d8c35b4e2b52e83c0/zookeeper-website/app/pages/_docs/docs/_mdx/developer/programmers-guide/sessions.mdx#L59-L112)).
  The session is the identity; expiry kills it, and the "penalty" is that the old identity is dead.
- **Penalising a holder that let a lease lapse**: I found no established pattern that penalises the *identity* of a
  lapsed holder in any of these. The mechanisms found are (i) loss of the lease, (ii) everyone (not only the lapsed
  holder) waiting a full lease duration measured on the observer's clock, (iii) jittered re-acquire on a fixed retry
  period (client-go), (iv) arrival-order queueing (etcd), (v) a dead session identity (ZooKeeper). Looked in: the
  files above; secondary literature not consulted.

**Inference (what this implies for the commit turn).** rask's handed turn differs from all of these in one way that
matters: its holder (a remote stock pylance client) sends no heartbeat and does not know it holds anything, so a
lapse cannot distinguish "came back late" from "vanished"; Kubernetes, etcd and ZooKeeper all detect a vanished
holder by a missed heartbeat from a holder that knows. The `COOLDOWN` keyed by caller identity has no precedent in
these systems; their closest equivalent is that a lapsed lease simply lapses and every candidate re-contends with
jitter. Keying a penalty by identity also fights zero trust's spirit when a pipeline's workers share one workload
identity: one worker's lapse withholds the preference from its siblings, which is a decision about a group made from
a credential that does not name the individual. The precedents point to keying by the grant itself (the handed
turn's token, as etcd keys by lease ID) and letting expiry be the whole consequence, with the store's clock (rask's
store-clock expiry already matches client-go's "measure on one clock" rule) deciding when. Whether to keep a
cool-down at all is a design choice these sources do not settle; they only show that none of them needed one.

## Could not confirm

- That FastAPI or Starlette documents "await sleep between short threadpool reads" as an idiom (searched both docs
  trees; found only the general async/sync rule).
- Lakekeeper: whether `try_commit_tables` closes its transaction before the back-off sleep; the HTTP status of
  `ErrorModel::internal`.
- Polaris: the HTTP status of a `RetryTimeoutException`; that its generated REST interfaces are non-reactive (so run
  on Quarkus workers).
- Iceberg: that `util/Tasks` sleeps the calling thread (not opened).
- Unity Catalog: the effective lock-wait bound (no setting found in `server/src/main`).
- Polly v7's rejection exception name (v7 sources not opened).
- Any lakehouse catalog or lease system that penalises a lapsed holder by identity (none found in the sources above).
