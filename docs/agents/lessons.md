# Lessons the estate taught

Traps that recurred on rask's live estate and in its tooling, each one measured at least once. They are working
knowledge for any agent or person on this repo; the decisions themselves live in `docs/adr/` and the standing rules
in `CLAUDE.md`. Add a line when a trap costs a session, and delete one when the code makes it impossible.

## Estate facts

- **Two kubeconfigs, one is dead.** The live estate is `/etc/rancher/k3s/k3s.yaml`; `~/.kube/config` is a stale kind
  cluster that still answers.
- **The object store is RustFS** (ADR 0168). `rask-rustfs-0` in rask's namespace serves the lakehouse in development,
  and every CI lane runs the same `rustfs/rustfs` tag; the RustFS pod in `htr-batch` is another team's. Measure store
  behaviour (policy, CAS, STS) on rask's fixture or a Dagger lane, and see `rask-rustfs` before reading its docs.
- **Current state is test data.** "Destructive on real data" is not a blocker before production; only owner-held facts
  and missing permissions are.
- **The observability seam is the OTel Collector.** The store behind it is swappable and out of scope; prove the
  exported signal, not the backend.

## Traps

- **A secret has two readers.** moving its delivery path breaks the outbound credential; the inbound door passing proves only half.
- **pkill matches its own command.** pkill/pgrep -f kill the shell; use the [b]racket trick.
- **One row, one commit, one deploy.** measured: 33 of 61 docs commits were register bookkeeping; tests/unit is 82% of suite cost; one image serves 10 deployments.
- **A gate is not a gate until it has failed.** mutation-check every new gate; twice in one day one could not fail.
- **Cite spec.yaml, not the prose.** namespace.md's REST paths contradict the OpenAPI in its own bundle.
- **A pipe eats the exit code.** a shell reports its LAST command's status; a pipe's filter or a trailing cleanup silently replaces it.
- **My own residue looks like a defect.** establish provenance before filing a live finding; four false diagnoses in one day.
- **Green tests cannot tell you it's allowed.** check the pod's permissions/policy before designing around an operation; an aborted pass reports zero too.
- **The secrets rule is an outcome, not a mechanism.** judge posture, not `secretKeyRef` counts; one bootstrap secret, everything else a reference.
- **Check what a number measures.** and the condition it was taken under: a figure from a setting nobody runs sizes the system for a world that does not exist.
- **Never measure from inside the thing you measure.** a kubectl-exec repro shares the cgroup; it corrupted the series and nearly OOMed the pod under study.
- **A range with no terminator overruns.** awk/sed ranges spill into the next record; bound by the next header or parse in Python.
- **glibc arenas are sized by the host, not the cgroup.** RSS climbing with a flat Python heap is arena fragmentation; count 64MB mappings before hunting a leak.
- **An empty override is not neutral.** a blank value beats the base, an empty map fails to remove from it; each merge layer decides what nothing means.
- **A mutation check cannot prove the wiring.** a test can prove a mechanism while the production global is owned by someone else.
- **Re-read the blockers you wrote.** verdicts I write get audited by nobody: blockers, measurements, and a "proved live" that was a warm local cache.
- **A double must carry the whole signature.** a narrow stand-in cannot see the change it exists to catch; 3x in one day.
- **Prove it where it runs, not only in a test.** if the cluster cannot show the fix, it is half-finished.
- **Credentials arrive through the lifespan.** exec'd python in a rask pod has no S3 secret; drive the HTTP door instead.
- **A prefix delete has no directories.** `rm --recursive a/b` also removes `a/b-other`; anchor with the trailing delimiter.
- **A gate on the innermost call proves nothing.** gate every hop; a defaulted parameter makes an un-wired chain look clean.
- **Fix the reading rule before the reading.** pre-register the threshold AND the precondition; a control baseline is what makes the second checkable.
- **A backtick is live in more places than you think.** prose is live syntax: a backtick RUNS in a Makefile recipe, ENDS a Go raw string, and a # inside a `\` continuation truncates the command.
- **A converge that exits 0 is not your deploy.** services run their own image stems; read back the pod's image, and an absent counter is a reset, not a fix.
- **A setting with two readers cannot be repointed.** grep every consumer first; two of three breakages had no local reproduction.
- **A failed commit does not stop the chain.** a reformatting hook fails the commit and the chain ships stale HEAD; a failed Dagger build with `;` still lets helm deploy an unpushed tag (rev 284). Gate deploy on build exit.
- **Write the conclusion from the rows you captured.** twice in one day a corroborating detail was never sampled; decide the field list when designing the experiment.
- **The easy half of a row usually is not.** three times in one day the unblocked part dissolved on reading the code; read the CONSUMERS before touching it.
- **An ack_floor has two fields.** consumer_seq counts deliveries, stream_seq names a position; query every NATS node, streams are R1.
- **A repair cannot confirm its own diagnosis.** granting the thing under test proves it was absent, never that its absence was a bug; read what DECIDES it first.
- **A duplicate YAML key is silent.** last-wins drops the earlier block; it failed a deploy and had killed secret rotation estate-wide.
- **Independent is not unreadable.** a key 8 pods can fetch identifies none of them; check who can READ, not just who can COMPUTE.
- **A blocker marker rots in place.** 8 of 11 re-read against code were free; the workable count tallies my stopping points, not the work.
- **The layers are not the suite.** `tests/unit tests/integration` is 2 of 21 testpaths; a shared-package change breaks its CONSUMERS' suites.
- **Verify what answers before you measure it.** a failed port-forward leaves the port to a scratch container that answers valid JSON.
- **The estate already has the tool.** twice in one day I rebuilt an instrument rask ships; its comment named the trap I then hit.
- **Absence in a narrow search is not absence.** five wrong claims in a day from greps of the wrong place, form, region, query shape, or my own output; a one-overlay render is the same mistake.
- **The environment is part of the measurement.** host-vs-container, default-vs-deployed, serial-vs-parallel, empty-vs-populated store; a check that reads a live system is data-dependent.
- **An unrun lane rots silently.** 3 lanes outside CI absorbed 3 estate-wide changes in silence; a gate with no caller gates nothing.
- **A read-only prompt does not make an agent read-only.** subagents amended my commits and tried a live `kubectl delete`; a prompt is not a capability boundary.
- **The estate cannot take a chart change.** helm upgrade dies at the 1 MiB Secret limit (98.3% full); Kueue off buys 75.5%, the floor is stored chart source.
- **Workflow args are not persisted.** a resume needs byte-identical args; put unit text in files, pass paths; rebuild a continuation from journal.jsonl.
- **A skeptic judges against the row's bar.** "refute if no caller impact" refuted 9 true findings; define real as closes-when, house rule, or caller.
- **An empty review is not a clean review.** 2 of 7 agents returned [] after only setup; require per-case `cases` + `not_run` and read them.
- **A restored mutant can keep running.** same-size mutant + same-second restore = stale .pyc still executes; PYTHONDONTWRITEBYTECODE=1 and purge __pycache__.
- **ru_maxrss survives exec.** a child of a big parent reports 0 MB; use VmHWM in a shell-started process, with a control.
- **A measured pool must outlive its buffers.** a proxy_memory_pool freed after a test segfaults the process when a cached buffer is freed; hold pools for the process.
- **A watch window must outlast the worst case.** LH-183 watcher expired 18 min before the recycle; capture `logs --previous` + pod JSON first when an event may have passed.
- **A resume reruns everything after the first miss.** workflow resume is prefix-cached; one early interrupted item reran 14 skeptics; hand-author a continuation for only the missing items.
- **Zero lines is not zero value.** chart renders and import-time values guard real behaviour with 0 run-phase lines; wave 1 deleted 12 such guards; red-check them before deleting.
- **Helm leaves drift it did not make.** a 3-way merge never repairs an out-of-band change to an unchanged manifest; diff live objects against `helm get manifest` when a rolled pod fails on something its spec did not change.
- **A terminating pod still answers.** wait until the pod is GONE before a fault injection; read the replacement by name after a rollout; assert the path, not just the end state.
- **Check the clock before a timestamp.** read `date -u` in the step that writes any time; helm/kubectl print local CEST (UTC+2), twice misstamped the Ledger 2026-10-04.
- **An fsGroup also locks the SA token.** 2026-10-10: fsGroup re-owns the projected token 0640; a privilege-dropping server cannot read it (broke ESO via OpenBao, rev 293); prefer per-volume 0444; fix every 0400 mount at once.
- **The map goes stale when rows close.** update #478 FOCUS NOW + Closed since with every close or reorder; 2026-10-11 it named 4 closed rows and no XC-085.
- **A scaled-down webhook still intercepts.** scaling an operator to 0 leaves its failurePolicy=Fail webhook; blocked every Deployment write for ~1 min (XC-126).
- **A Dagger lane uploads the whole host tree.** `+defaultPath="/"` sends ~15 GB here and looks hung for 30+ min; run lanes with --src a clean `git ls-files` copy.
- **Build the local loop before the CI round.** 2026-10-12: seven blind 30-40 min CI rounds; a local Dagger k3s found the cause in minutes; reproduce first.
- **The node runs out of inotify before memory.** 2026-10-14: root held 127 of 128 instances (one per container, the containerd shim OOM watch); the Dagger engine dnsmasq exited 5 and builds died on DNS; `make k3s-install` sets 8192.
- **A RustFS admin route refuses a bare `?`.** a call with an empty query string answers 400 "failed to decode query"; send no `?` when there are no parameters (`scripts/rustfs_admin.py`, XC-083, found against a real RustFS 1.0.3, 2026-10-18).
- **A new required store field restarts the first pods.** a pod that boots before the dev seed writes the field fails closed until the seed lands (4 restarts, about 2 min, rev 32); the old pods serve meanwhile, so read the previous log before calling it a fault.
- **A port-forward hides a keep-alive close.** a server's close of an idle connection reaches the client only as its next request fails (`RemoteDisconnected`, XC-094 on the kind lane, 2026-10-17; again 2026-10-18, a scratch tenant's teardown after 7-8.5 s idle); every e2e client closes each request (`byo_scenario.CLOSE_EACH_REQUEST`), since a POST must not be retried.
- **An empty JetStream stream has no sequence 0.** a fresh stream reports 0 at both ends and a get of sequence 0 answers 400; read by sequence only while `messages` > 0 (XC-094, 2026-10-17).
- **A stock client may decline a spec op itself.** pylance's `RestNamespace` implements 51 of the 54: `UpdateFieldMetadata`, `BatchCreateTableVersions` and `BatchCommitTables` fall to the base class, which answers code 0 and sends nothing; a probe read that as the catalog declining (XC-092, 2026-10-17). Drive those through `lance_namespace_urllib3_client`.
- **Ray under `uv run` starts no worker.** Ray 2.58 relaunches each worker with the driver's own `uv run` command line, which under pytest never starts: five retries and a nine-minute wait, against 2.9 s with `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` (XC-092, 2026-10-17).
- **A namespace and a table share an id.** the `dir` backend keeps one object-id space, so a table created at a child namespace's id answers 409 code 14 ("concurrently created"), not TableAlreadyExists (XC-092, 2026-10-17).
- **pylance reports an unreachable host as a door's 503.** its REST client answers `ServiceUnavailableError` (code 17) for a refused connection too, so a probe that must tell the door's verdict from no door at all sends raw HTTP (XC-095, 2026-10-17).
- **A describe names no version unless asked.** the `dir` backend, and the catalog's DescribeTable that passes the flag through, report `version: None` without `load_detailed_metadata=True` (XC-095, 2026-10-17).
- **A port-forward outlives its pod.** `kubectl port-forward svc/<s>` binds one pod and stays up after that pod dies, dropping every connection it accepts unanswered, so a lane that restarts pods forwards to the Service's ready pod and starts the forward again when that pod changes (`scripts/e2e_stack.sh` `forward`, XC-095, 2026-10-17).
- **`bao status` exits 2 for a sealed store,** with the same JSON it prints for an unsealed one, so a caller reads `sealed` from the JSON and accepts both codes (XC-095, 2026-10-17).
- **A Dapr sidecar's bus connection does not survive a long outage.** past nats.go's 60 reconnect attempts the sidecar's pub/sub is dead until it restarts, while its pod stays Ready; `service_kit.bus_watchdog` restarts it (`rask-dapr` § Traps, XC-095, 2026-10-17).
- **A live drill dies with the session that started it.** A drill run from an agent session's background shell is killed when the session ends, and a killed drill skips its `finally`: XC-095's issuer drill left Dex's Service selector cut on the live estate until it was restored by hand. Run a live drill detached (`setsid nohup … &`) so it reaches its `finally` whatever happens to the session (XC-095, 2026-10-17).
- **pytest takes a conftest option's value for a path.** It parses the command line before it loads the conftest that
  defines an option, so `-m phase1 --keep-drive PATH` with no test path took PATH for the run's own and never loaded
  `tests/e2e-py/conftest.py` ("unrecognized arguments"); a flag with no value is unaffected. `scripts/e2e_live.sh`
  roots a pathless run at `tests/e2e-py` (XC-090, 2026-10-17).
- **A CI gate no local gate mirrors goes red unnoticed.** Rows land straight on `main`, so `dagger call charts` (ms-charts) failed on every push from LH-063 to XC-092: its render args lacked the `auth.oidc.connectors` LH-063's guard began to require. A change that adds a render guard runs `dagger call charts` locally before it lands (XC-095, 2026-10-17).
- **A pinned pre-release store can refuse a valid request.** RustFS 1.0.0-alpha.60, the governed Dagger lanes' store,
  answered pyarrow 25's multipart `UploadPart` with `XAmzContentSHA256Mismatch`, so the catalog's boot write of a
  protection record failed, lineage was never granted its provenance tables, and `governance-chain` was red on every
  push for two days. RustFS 1.0.1 and 1.0.3 take the same write: run rask's own writer against the pinned and a current
  release before changing rask (XC-033, 2026-10-17; XC-163, 2026-10-18).
- **A dev-mode OpenBao prints its root token to its log.** `server -dev` writes `Root Token:` and `Unseal Key:` to stdout
  even with `-dev-root-token-id` (openbao 2.2.0 `command/server.go:2720,2732`), so any log reader holds the store; an
  in-memory server from a config file starts uninitialized and logs only that a root token was generated (XC-165,
  measured 2026-10-18).
- **Helm evaluates both arms of `ternary`, and `and` returns an operand, not a bool.** An arm that reads a key a values
  map lacks fails the render even when its condition is false, and `and $missing true` hands `ternary` a nil it refuses
  ("expected bool"); compute the arm inside an `if`, and default a flag (`| default false`) before testing it (XC-165).
