"""§6 Q12(b) — a DLQ handler must ack UNCONDITIONALLY, and this pins that it does.

WHY IT IS ABSOLUTE. A dead-letter topic holds messages that have ALREADY exhausted their delivery
retries. Returning RETRY from the handler that parks them asks Dapr to redeliver a message whose whole
reason for being there is that redelivery did not work — so it re-queues forever, at whatever the
component's backoff is, for a payload nothing will ever process. The DLQ stops being a parking lot and
becomes a spin loop; and because each park is also COUNTED, the metric that operators watch inflates
without bound while nothing new is actually failing.

`notifications/api/dlq.py` states the rule in its own module docstring. This makes it structural for
every DLQ handler in the estate, because the rule is the same in all three and a fourth is likely.

Parsed with `ast` rather than driven, deliberately: driving each handler needs a Dapr envelope and an
app per service, and what has to be true is a property of the FUNCTION — that no reachable return can
carry RETRY. A structural check states that directly, and cannot pass because a fixture happened to
take the success branch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]

#: Every DLQ handler module in the estate. Listed rather than globbed for `dlq` so a file named for
#: something else cannot quietly drop out of the gate.
DLQ_MODULES = (
    "services/lineage/src/lineage/api/dapr.py",
    "services/medallion/src/medallion/api/dlq.py",
    "services/notifications/src/notifications/api/dlq.py",
)


def _dlq_functions(source: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function in the module whose route or name marks it as a DLQ parking handler."""
    tree = ast.parse(source)
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        blob = ast.unparse(node)
        if "dlq" in node.name.lower() or "dlq" in blob.lower()[:400]:
            found.append(node)
    return found


@pytest.mark.parametrize("module", DLQ_MODULES)
def test_a_dlq_handler_never_returns_RETRY(module: str) -> None:
    """The invariant. A RETRY here re-queues a message that already exhausted its retries."""
    path = REPO / module
    assert path.exists(), f"{module} moved — update this gate WITH the move, not after it"
    source = path.read_text(encoding="utf-8")

    handlers = _dlq_functions(source)
    assert handlers, f"no DLQ handler found in {module} — the gate would pass vacuously"

    for fn in handlers:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            returned = ast.unparse(node.value)
            assert "RETRY" not in returned.upper(), (
                f"{module}:{node.lineno} — {fn.name} can return {returned!r}. A DLQ handler that RETRYs "
                f"re-queues a message whose retries are already exhausted: it spins forever and inflates "
                f"the parked counter while nothing new is failing."
            )


def test_the_estate_has_no_UNGATED_dlq_handler() -> None:
    """The list above is hand-kept, so this catches a fourth handler landing without joining it — the
    same drift that let `services/catalog/tests` sit collected-by-nothing.

    Keyed on the DECORATOR (`@dapr_app.subscribe(..., topic=<something dlq>)`), not on the word "dlq"
    appearing near "subscribe". The first version of this used the loose text heuristic and flagged
    `catalog/core/lineage_emit.py` and `medallion/api/train.py`, neither of which handles a dead letter —
    a gate that cries wolf gets deleted, which is worse than not having it.
    """
    import ast

    subscribers: set[str] = set()
    for path in (REPO / "services").rglob("*.py"):
        if "test" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a file that does not parse is not a handler
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not ast.unparse(dec.func).endswith("subscribe"):
                    continue
                kwargs = {kw.arg: ast.unparse(kw.value) for kw in dec.keywords}
                # A DLQ subscription names a dlq topic or parks on the /dlq-event route.
                if "dlq" in (kwargs.get("topic", "") + kwargs.get("route", "")).lower():
                    subscribers.add(str(path.relative_to(REPO)))

    assert subscribers, "no DLQ subscription found anywhere — this gate would pass vacuously"
    unlisted = subscribers - set(DLQ_MODULES)
    assert not unlisted, f"these files subscribe to a DLQ topic but are not in DLQ_MODULES: {sorted(unlisted)}"
