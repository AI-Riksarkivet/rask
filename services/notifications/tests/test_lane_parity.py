"""THE TWO INGRESS LANES MUST CARRY THE SAME TARGETING. A guard for the defect class, not the instance.

`reconcile()` accepted `watchers` and `push`, `reconcile_cron` passed both, and the call to
`ingest_run_event` forwarded NEITHER. So on the feed lane — the only lane ingest, Ray TRAIN and every
external OpenLineage producer ever reach — `audience_for` silently degraded to the author alone and
`fan_out` received no `push`, so no project watcher was ever told and no email or Slack was ever sent.
The author's row still landed and the tick still logged `lineage_feed_reconciled`, so the plane looked
healthy from every angle except the one nobody checked.

The instance is fixed. This guards the CLASS: both parameters are optional, so any future lane can omit
them exactly as silently.

WHY A SOURCE GUARD RATHER THAN REQUIRED KEYWORD PARAMETERS. Dropping the defaults on `ingest_run_event`
is the stronger fix — the type system would refuse the omission outright — and it is the one the audit
recommended. It is not done here because `ingest_run_event` has 58 call sites and 55 of them are tests
that pass neither, so the change is ~55 mechanical edits to test files in a session that has already
shipped one regression from a blanket edit. The production surface is TWO call sites; this guard covers
both and any third, today, with no churn. Dropping the defaults remains the right follow-up, and doing it
will make this test redundant — delete it then.
"""

import re
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src" / "notifications"

#: The call, and the window its keyword arguments live in. Ten lines is generous: the longest production
#: call spans seven.
_CALL = re.compile(r"\bingest_run_event\(")
_WINDOW = 10


def _production_call_sites() -> list[tuple[str, int, str]]:
    """Every `ingest_run_event(` call under the service's own source, with its argument window."""
    sites: list[tuple[str, int, str]] = []
    for py in sorted(SRC.rglob("*.py")):
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            if not _CALL.search(line) or line.lstrip().startswith(("def ", "async def ")):
                continue
            sites.append((py.name, i + 1, "\n".join(lines[i : i + _WINDOW])))
    return sites


def test_there_is_at_least_one_call_site_to_check() -> None:
    """The floor. A guard that silently matches nothing is the exact failure this file exists to prevent —
    the invariant it replaces spent its life green because its pattern matched no call site in the estate."""
    assert len(_production_call_sites()) >= 2, "expected the bus and feed lanes to both call ingest_run_event"


def test_every_lane_forwards_the_watcher_lookup() -> None:
    """Omitting it does not fail — it silently narrows the audience to the author."""
    missing = [f"{name}:{line}" for name, line, window in _production_call_sites() if "watchers=" not in window]
    assert not missing, (
        f"these ingress lanes call ingest_run_event WITHOUT `watchers=`: {missing}. The call succeeds and "
        "`audience_for` degrades to the author alone, so every project watcher is silently never told. "
        "Pass `watchers=watchers` (or `watchers=None` if the lane genuinely has no lookup, and say why)."
    )


def test_every_lane_forwards_the_channel_push() -> None:
    """Omitting it does not fail — it silently sends no email and no Slack for that lane's runs."""
    missing = [f"{name}:{line}" for name, line, window in _production_call_sites() if "push=" not in window]
    assert not missing, (
        f"these ingress lanes call ingest_run_event WITHOUT `push=`: {missing}. The inbox row still lands, "
        "so the bell looks correct while every channel for that lane stays silent. Pass `push=push` (or "
        "`push=None` deliberately, and say why)."
    )
