"""The determinism gate banned env reads at MODULE scope, which is not where the hazard lives.

`test_replay_hygiene.py::test_the_workflow_module_reads_NO_env_at_import` walks the AST and refuses
`os.getenv` / `os.environ` OUTSIDE any function. That catches a real defect — a module constant is
fixed per POD, not per RUN — but it is the weaker half. An `os.getenv` inside a WORKFLOW BODY passes
it untouched, and that is the one the plane already paid for: `RunLimits` records the exact break,
`if max_run_hours > 0` decides whether a durable timer exists, so a rolling deploy that changed the
variable between a run's first execution and its replay produced an action stream the history does
not match. `resolve_limits` exists to pin those numbers in history instead.

So the estate had a gate that would have let the defect it was written about come straight back.

The distinction that makes this precise, and why the gate cannot simply ban env reads everywhere:
an ACTIVITY may read env freely — its result is recorded in history, so every replay sees the value
the first execution saw. A WORKFLOW BODY may not, because it is re-executed from scratch on every
replay against whatever the environment says now.

The targets are derived from `WORKFLOWS`, not hard-coded. A hard-coded pair is a gate that silently
stops covering the third workflow somebody adds.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from ingest import workflow as wf_module
from ingest.replay_guard import env_reads_in_workflow_bodies


SRC = Path(wf_module.__file__)


def _names() -> set[str]:
    """The registered workflow bodies, by name, off the module's own tuple."""
    return {fn.__name__ for fn in wf_module.WORKFLOWS}


class TestTheRealModuleIsClean:
    def test_no_registered_workflow_body_reads_env(self) -> None:
        offenders = env_reads_in_workflow_bodies(SRC.read_text(encoding="utf-8"), _names())
        assert offenders == [], (
            f"{offenders} read the environment inside a workflow body — a replay re-executes that "
            f"line against whatever the value is NOW, so a rolling deploy makes history disagree. "
            f"Resolve it in an activity and carry the result on the spec, as `resolve_limits` does."
        )

    def test_the_gate_knows_which_functions_to_check(self) -> None:
        """Derived, not hard-coded: a gate that names its targets stops covering the next one."""
        assert _names(), "WORKFLOWS is empty — this gate now checks nothing"


class TestTheGateActuallyCATCHES:
    """The half that matters. A detector nobody has seen fail is a detector nobody knows works, and
    this one exists precisely because its predecessor passed the case it was meant to stop."""

    @pytest.mark.parametrize(
        "body",
        [
            "    hours = os.getenv('RASK_INGEST_MAX_RUN_HOURS', '0')\n    yield ctx.call_activity(x)",
            "    hours = os.environ['RASK_INGEST_MAX_RUN_HOURS']\n    yield ctx.call_activity(x)",
            "    hours = os.environ.get('RASK_INGEST_MAX_RUN_HOURS')\n    yield ctx.call_activity(x)",
            "    if os.getenv('FLAG'):\n        yield ctx.call_activity(x)",
        ],
    )
    def test_it_refuses_an_env_read_inside_a_workflow_body(self, body: str) -> None:
        source = f"import os\n\n\ndef ingest_run(ctx, payload):\n{body}\n"
        assert env_reads_in_workflow_bodies(source, {"ingest_run"}) == ["ingest_run"]

    def test_it_finds_a_read_nested_inside_control_flow(self) -> None:
        """The defect will not present itself at the top of the function."""
        source = (
            "import os\n\n\n"
            "def ingest_run(ctx, payload):\n"
            "    for chunk in payload['chunks']:\n"
            "        try:\n"
            "            if chunk:\n"
            "                limit = os.getenv('X')\n"
            "        except Exception:\n"
            "            pass\n"
            "    yield ctx.call_activity(x)\n"
        )
        assert env_reads_in_workflow_bodies(source, {"ingest_run"}) == ["ingest_run"]


class TestItDoesNotOverREACH:
    def test_an_ACTIVITY_may_read_env(self) -> None:
        """The whole point of resolving in an activity: its result is recorded, so every replay sees
        the value the first execution saw. Banning it there would ban the fix."""
        source = "import os\n\n\ndef resolve_limits(ctx, payload):\n    return {'h': os.getenv('X')}\n"
        assert env_reads_in_workflow_bodies(source, {"ingest_run"}) == []

    def test_a_module_constant_is_the_OTHER_gates_job(self) -> None:
        """Not silently double-covered — test_replay_hygiene owns module scope, and two gates
        reporting one defect is how a fix gets applied to the wrong one."""
        source = "import os\n\nLIMIT = os.getenv('X')\n\n\ndef ingest_run(ctx, payload):\n    yield ctx.call_activity(x)\n"
        assert env_reads_in_workflow_bodies(source, {"ingest_run"}) == []

    def test_an_unregistered_helper_is_not_a_workflow_body(self) -> None:
        source = "import os\n\n\ndef _helper():\n    return os.getenv('X')\n"
        assert env_reads_in_workflow_bodies(source, {"ingest_run"}) == []


def test_the_gate_cannot_go_vacuous_on_a_parse_failure() -> None:
    """A detector that returns [] for unparseable source reports every file as clean."""
    with pytest.raises(SyntaxError):
        env_reads_in_workflow_bodies("def broken(:\n", {"ingest_run"})


def test_the_real_module_still_parses_and_declares_bodies() -> None:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    found = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    assert _names() <= found, "WORKFLOWS names a function this module does not define"
