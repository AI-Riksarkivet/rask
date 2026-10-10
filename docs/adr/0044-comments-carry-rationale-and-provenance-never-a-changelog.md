# 0044. Comments carry rationale and provenance, never a changelog of the prose (2026-08-30, owner ruling)

**Decision.** Module prose sorts into three kinds. Two stay; one is banned in NEW code and left alone
where it already stands.

| Kind | Example | Verdict |
| --- | --- | --- |
| **(1) RATIONALE** — why the shape is load-bearing; the invariant; the failure the design avoids | *"SORT AND CAP FIRST, VALIDATE SECOND. The old order — validate every job, then sort — built a list of every job in the cluster before the cap could apply."* | **STAYS.** This is the most valuable prose in the estate. |
| **(2) PROVENANCE** — a dated measurement, a named pin, an owner ruling with its date | *"Measured 2026-08-26: the ray-cluster export alone takes 238 s."* / *"Pinned by `tests/unit/test_invariants.py::test_…`"* | **STAYS, and keeps its measurement.** Stripping the date turns a fact into a claim. |
| **(3) HISTORY OF THE PROSE** — a comment whose subject is a previous comment | *"This docstring used to say the lookup was cached."* / *"The comment above claimed the lock was held here, which was wrong."* | **BANNED IN NEW CODE.** |

**The ban is FORWARD ONLY.** The 35,279 prose lines already in `packages/` + `services/` are not to be
drained. A retroactive sweep is a large unreviewable diff whose most likely casualty is exactly the
kind-(2) measurements the prose exists to preserve, and the estate has no way to review a diff that
size line by line.

**What replaces a kind-(3) comment.** The estate already has a rule for prose a change falsifies:
**rewrite the sentence, do not append to it** — a correction bolted on below leaves two claims standing
and readers take the wrong one. Kind (3) is what that rule produces when it is half-applied: the old
sentence gets annotated instead of replaced. So the fix is always the same — state what is true now.
If the *past shape is the reason* for the present one, say that about the **code** ("the old order
built the whole list before capping"), never about the **comment** ("this line used to say").

**Rationale.** Measured 2026-08-30 with a tokenize pass (comment + docstring lines against non-blank
code lines) at `621cd1a8`: `packages/` + `services/` source is **46.1% prose** (35,279 / 76,471), and the
`services/ingest` scope the audit flagged is **60.8%** across its seven core modules — up from the 60%
measured 2026-08-28 and the 52% measured 2026-08-24. Quote that figure with its scope attached: the
whole of `services/ingest/src` is **57.0%** (4,656 / 8,174 over 29 files), and "ingest is 61%" is true
only of the seven modules the audit named, not of the service.

Kinds (1) and (2) earn their share: this estate's recurring defect is a claim nobody can check, and
a dated measurement or a named pin is the cheapest possible check. Kind (3) earns nothing. It is a changelog of a file that already has one — git — and
it grows monotonically, because every correction adds a layer without removing the one beneath it.

**The gate, and its honest limit.** `scripts/comment_history_gate.py` checks **added lines only**
(`--staged` in `prek.toml`, `--base <ref>` in CI). It is diff-aware by design: a whole-tree lint would
be red on its first run against prose nobody is permitted to change, and a gate that is red by
construction is one everybody learns to ignore.

It fires on exactly one thing — **prose whose subject is prose** ("used to say", "no longer claims",
"this docstring was wrong"). That form is mechanically safe to ban because deleting it loses nothing
about the code.

It **cannot** catch the other half of kind (3), a changelog of past *code* decisions, and does not try.
Nothing in the wording separates these two:

```python
# The old order — validate every job, then sort — built a list of every job before the cap.   # KEEP (1)
# Sorting moved above validation on 2026-08-12 when the profile came back.                    # BAN  (3)
```

Only a reader can tell them apart, so they stay a review concern. **Passing the gate is not
conformance with this rule** — it is a floor under it. Its advisory whole-tree count
(`--report`) exists to measure the standing population, never to gate a change.

**Its measured error rate, so nobody has to take the above on trust — and this figure was wrong the
first time it was published.** `--report` over every tracked gated file on 2026-08-30 returns **53**
lines. The original ruling said "52 are genuine, one is a false positive"; an adversarial re-read of
all 53 found **at least two** false positives and a third that is right for the wrong reason:
`ObjectBrowser.svelte:50` ("the only sentence that said what was actually wrong" is the server's
problem+json detail, not a comment); `models/e2e/shell.spec.ts:14` (a present-tense statement of what
the test asserts about the UI — `no longer` alone carried the match); and `test_publish_saga.py:491`,
where the flag is defensible but the subject is a fixture literal, so the gate's stated reason ("a
comment whose subject is a previous comment") is false.

Budget roughly **one in twenty**, and treat that as a FLOOR rather than a measurement: separating
kind-(3) history from kind-(1) rationale is exactly the reader's judgement the gate cannot make, which
is why it is advisory and forward-only. None of the three is worth chasing — each narrowing overfits
the pattern to one site, and forward-only the gate never reads these lines again.

Recording the correction rather than quietly restating the number, because a gate that overstates its
own precision is the specific failure this section elsewhere calls the estate's recurring one.

Over the 100 commits before this ruling the gate would have flagged **15** added lines, every one of
them genuine — so the habit it addresses is live, not hypothetical, and the gate will have work to do
from its first commit.
