# 0071. `compaction_mode` is not a measure of where bytes moved (2026-09-21)

**The correction.** A maintenance OOMKill (`rask-maintenance`, 6 restarts, exit 137, 512Mi) was
attributed to in-pod compaction on the strength of the sweep's own per-dataset line: 2,207 of 4,276
outcomes in the killed container carried `mode='in_pod'`, and `chart/templates/maintenance-worker.yaml`
states that `compact_files()` makes the pod's ceiling "a function of the largest table anyone owns".
The inference was wrong.

`DatasetResult.compaction_mode` **defaults** to `"in_pod"` (`optimize.py`) and is overwritten only when
the distributed path SUCCEEDS. So a dataset refused before the rewrite gate, and a dataset with nothing
to compact, both report `in_pod` having opened nothing heavy. Measured on the deployed estate the same
day: of 372 `in_pod` outcomes, **zero removed a single fragment**, and 150 of them were refusals
(shallow clone / protected base) which skip both paths by design. The counter answers "did the
distributed path succeed", never "where did bytes move" — and `test_compaction_runs_off_the_pod.py`
already said so in one line nobody read as a warning: *"the mode field records what actually rewrote
bytes, and nothing did"*.

**The fix that came out of it stands on different grounds, and was verified on its own.** Two doors
resolved one dataset's identity differently: `sweep.maintain_one_item` took the path first and the
stamp second and vended the write credential under that answer, while `optimize.compact_one` gated the
off-pod rewrite on the in-schema stamp alone. Of 151 distinct unstamped datasets one tick reported,
`table_id_from_location` answers for 123 (81%) — the catalog could have planned every one and was never
asked, while their bytes were signed by a credential vended under exactly that id. The id is now
resolved once and threaded to both doors. Observed after deploy: **523 outcomes on the distributed path
against 3** the gate should have taken and did not.

**The OOM's cause is therefore still open.** The leading candidate is `_probe_before_vending`, which
opens EVERY dataset in the sweep — its own docstring records "the sweep walks 552 datasets a tick and
this probe already opens every one of them that is about to be vended for". That is a per-tick cost
proportional to the estate rather than to the work, and it is not what the mode counter measures.

**Why this is recorded rather than quietly corrected.** The falsified sentences shipped in a comment and
a test docstring and have been rewritten in place, per the prose rule. What does not survive a rewrite
is the reasoning error itself, which is reusable: a field whose default value is also a meaningful
value cannot be counted. Anything reading `compaction_mode` as a workload measure — a dashboard, an
alert, a capacity argument — is reading a default.
