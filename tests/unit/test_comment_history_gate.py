"""The three cases that ARE the comment rule, plus the two properties that keep the gate usable.

The rule (docs/DECISIONS.md, owner 2026-08-30) sorts module prose into RATIONALE (keep), PROVENANCE
(keep, with its measurement) and HISTORY-OF-THE-PROSE (banned in new code). A gate that cannot tell
the third from the first two would be rejected on its first false positive, so the accept cases are
load-bearing here in exactly the way the reject case is.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("comment_history_gate", REPO_ROOT / "scripts" / "comment_history_gate.py")
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["comment_history_gate"] = gate
_SPEC.loader.exec_module(gate)


# --- kind (3): HISTORY OF THE PROSE — must be REJECTED ------------------------------------------

HISTORY_SAMPLES = [
    '"""Resolve the active head.\n\n    This docstring used to say the lookup was cached; it never was.\n    """\n',
    "# The comment above claimed the lock was held here, which was wrong.\n",
]


@pytest.mark.parametrize("body", HISTORY_SAMPLES)
def test_a_comment_whose_subject_is_a_previous_comment_is_rejected(body: str) -> None:
    findings = gate.scan_source("services/example/src/example/mod.py", f"x = 1\n{body}")
    assert findings, f"kind-(3) history prose passed the gate: {body!r}"


def test_a_history_phrase_wrapped_across_two_comment_lines_is_still_caught() -> None:
    """A comment reflowed at 160 columns must not buy an exemption."""
    source = "x = 1\n# The reader's docstring used to\n# promise a fallback that does not exist.\n"
    findings = gate.scan_source("services/example/src/example/mod.py", source)
    assert findings, "a phrase split across two comment lines evaded the gate"
    assert findings[0].line == 2, "a wrapped phrase must be reported once, on the line it starts on"


# --- kind (1): RATIONALE — must be ACCEPTED ------------------------------------------------------

RATIONALE_SAMPLES = [
    "# SORT AND CAP FIRST, VALIDATE SECOND. The old order — validate every job, then sort — built a\n"
    "# list of every job in the cluster before the cap could apply.\n",
    "# Opt-IN, not opt-out: a service that publishes no lineage must be able to say no, and the\n# default has to be the safe half of that.\n",
]


@pytest.mark.parametrize("body", RATIONALE_SAMPLES)
def test_rationale_that_explains_a_past_shape_is_accepted(body: str) -> None:
    findings = gate.scan_source("services/example/src/example/mod.py", f"x = 1\n{body}")
    assert not findings, f"kind-(1) rationale was rejected: {[f.render() for f in findings]}"


# --- kind (2): PROVENANCE — must be ACCEPTED -----------------------------------------------------

PROVENANCE_SAMPLES = [
    "# Measured 2026-08-26: the ray-cluster export alone takes 238 s, which is why this path caches.\n",
    "# Pinned by tests/unit/test_invariants.py::test_every_declared_application_is_provided.\n",
]


@pytest.mark.parametrize("body", PROVENANCE_SAMPLES)
def test_dated_measurements_and_named_pins_are_accepted(body: str) -> None:
    findings = gate.scan_source("services/example/src/example/mod.py", f"x = 1\n{body}")
    assert not findings, f"kind-(2) provenance was rejected: {[f.render() for f in findings]}"


# --- the properties that make a FORWARD gate a forward gate --------------------------------------


def test_the_gate_ignores_code_that_merely_contains_the_words() -> None:
    """Only comment and docstring lines are read — a string literal or an identifier is not prose."""
    source = 'MESSAGE = "this docstring used to say the lookup was cached"\n'
    assert not gate.scan_source("services/example/src/example/mod.py", source)


def test_only_added_lines_are_gated_so_existing_prose_is_untouched() -> None:
    """The ban is FORWARD ONLY: the same file gates on a new line and stays silent on an old one."""
    source = "x = 1\n# This docstring used to say the lookup was cached.\n"
    path = "services/example/src/example/mod.py"
    assert gate.scan_source(path, source, only_lines={2}), "an ADDED history line must be caught"
    assert not gate.scan_source(path, source, only_lines={1}), "an untouched history line must NOT be gated"


def test_markdown_is_out_of_scope_because_docs_keep_a_trail() -> None:
    assert not gate.is_gated("docs/DECISIONS.md")
    assert gate.is_gated("services/example/src/example/mod.py")
    assert gate.is_gated("frontend/microfrontends/home/src/lib/x.ts")
