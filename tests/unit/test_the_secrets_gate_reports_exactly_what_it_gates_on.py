"""The secrets scan's two passes must filter identically, and the filter must stay narrow.

`dagger call scan-secrets` runs trufflehog twice: once reporting everything for a human, once with
`--fail` as the gate. They differ only in `--results`, and that is the whole design — a reviewer sees
the unverified matches, the build fails only on live credentials.

A DETECTOR EXCLUDED FROM ONE PASS AND NOT THE OTHER BREAKS BOTH HALVES: exclude it only from the gate
and hundreds of findings stay in the report nobody can read; exclude it only from the report and the
build fails on something no reviewer was shown. So the exclusion is one constant and both passes take
it from there.

WHY THERE IS AN EXCLUSION AT ALL, measured 2026-09-24: the gating pass returned 620 findings and all
620 were Lob, whose test keys are `test_`-prefixed — the name of every pytest function in this repo's
history (`test_every_lineage_producer_uri_resolves` among them). The job had failed on every run since
it landed on 2026-08-05, fifty days of a blocking gate that gated nothing.

The list is kept SHORT on purpose. A suppression file that grows one entry per noisy scan ends up as
the real policy, and nobody reads it either.
"""

from __future__ import annotations

import re
from pathlib import Path


_SCAN = Path(__file__).resolve().parents[2] / ".dagger" / "scan.go"
_CONST = re.compile(r'ExcludedDetectors\s*=\s*"([^"]*)"')
_TRUFFLEHOG = re.compile(r'"trufflehog git [^"]*"')


def test_both_trufflehog_passes_take_the_same_exclusions() -> None:
    body = _SCAN.read_text(encoding="utf-8")
    invocations = _TRUFFLEHOG.findall(body)

    assert len(invocations) == 2, f"expected the report pass and the gate pass, found {len(invocations)}: {invocations}"
    for call in invocations:
        assert "--exclude-detectors=%[2]s" in call, f"a trufflehog pass does not take the shared exclusion constant: {call}"


def test_the_exclusion_list_stays_a_short_argued_list_not_a_suppression_file() -> None:
    found = _CONST.search(_SCAN.read_text(encoding="utf-8"))

    assert found, "ExcludedDetectors is gone — either the exclusion moved or the scan stopped filtering"
    entries = [e for e in found.group(1).split(",") if e]
    assert entries, "the exclusion constant is empty, so the gate is back to measuring the repo's vocabulary"
    assert len(entries) <= 3, f"{len(entries)} detectors excluded: {entries}. Past two or three this is a baseline, and a baseline is a policy nobody reads."
