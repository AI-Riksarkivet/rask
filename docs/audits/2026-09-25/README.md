# Audits of 2026-09-25 — what was checked, what was found, where it went

Seven audits ran as multi-agent workflows on 2026-09-25; this index lists the reports whose findings are
still cited. Each report below is the audit's own synthesis,
kept verbatim so every finding keeps its citation; the one edit is that a pointer into a retired register now names
the row in `open_backlog_left_new2.md` (or states the rule it pointed at). Their findings were turned into rows of
`open_backlog_left_new2.md` (which superseded `open_backlog_left.md` and `open_backlog_left_new.md` the
same day); this folder is the evidence those rows cite.

Sources read: the vendored `lance_docs/` (see `lance_docs/PROVENANCE.md` for its pins), Lakekeeper at
`lakekeeper/lakekeeper@a58e401` and `lakekeeper/lakekeeper-charts@cffd5b7`, pylance 12.0.0 and 11.0.0,
and the rask tree as it stood that day. Every measurement ran on a local filesystem or moto unless a
report says otherwise; none ran against the production S3.

| # | Report | What it did | Verified how | Where the findings went |
| --- | --- | --- | --- | --- |
| 03 | [lance_docs full audit](03-lance-docs-full-audit.md) | Every line of `lance_docs/` in 16 chunks, each with an adversarial verifier; a completeness critic and gap readers; the branch-write question (D3) answered from the spec | Chunk verifiers; gap-reader findings are marked UNVERIFIED | LD00–LD45 mapped to rows; D3 ruled (a) from its answer |
| 04 | [Lakekeeper deep-read](lakekeeper-deep-read/) | Nine domains read in full against rask: [authn](lakekeeper-deep-read/authn.md), [authz](lakekeeper-deep-read/authz.md), [storage and vending](lakekeeper-deep-read/storage-vending.md), [secrets](lakekeeper-deep-read/secrets.md), [events](lakekeeper-deep-read/events.md), [governance](lakekeeper-deep-read/governance.md), [provenance and audit](lakekeeper-deep-read/provenance-audit.md), [resilience](lakekeeper-deep-read/resilience.md), [chart](lakekeeper-deep-read/chart.md) (all 42 files) | Each report lists the files it read in full | The *How* of each carried row names the Lakekeeper parallel from these reports, translated onto rask's stack |
| 05 | [pylance 12 mixed-file-version plan](05-pylance12-mixed-version-plan.md) | Measured which write operations mix data-file versions on pylance 12, every rask write site, every commit door, detection and healing | A 26-case predicate matrix against Lance's own commit; two review waves | Shipped as 9563eb87 (deployed); its deferred S9 items are rows in the new backlog |

[`kueue-handover-note.md`](kueue-handover-note.md) is the note for the htr-batch team that D11's handover (XC-049) waits on.

The seventh workflow was the backlog re-audit itself: every one of the 197 open rows re-measured
against the code by an auditor and a skeptic, with the reports above as input. Its output is
`open_backlog_left_new2.md`, not a report here.

**What is not covered, stated so it is not read as covered.** The `lance_docs` chunk for the branch/tag
spec, table format and storage layout (`file_format.md` 2696–3251) was read only by an unverified gap
reader after its chunk agent died; ten of eighteen coverage gaps the critic named were left unread
(images, `.pages`, protobuf placeholders, FTS query models, the dir compatibility mode and MemWAL
internals among them).
