"""Phase 1's soak: a day after the acceptance, the estate still reads its drive as the criteria did (XC-090).

The acceptance holds one drive of the bring-your-own scenario to the criteria the moment it ends. An estate left running
goes on acting on what that drive made: maintenance sweeps its tables, the outbox relays and the notifications reconciler
re-read the feed and re-deliver, sidecars reconnect, secrets are re-read. An acceptance run keeps its drive with
`--keep-drive PATH` (`kept_drive`), and a day later this module reads that drive again and holds the estate to four
claims, reported together:

1. Lance holds exactly the commits the drive made, each carrying the run it named and who committed it, and the edge
   list exactly one edge for each input those commits name (criterion 1);
2. lineage's feed names every version on every ref of the drive's tables, and holds exactly the rows it held when the
   drive was kept (criterion 4): no version below the tip is unannounced, and nothing was recorded again;
3. nobody has been told anything of the drive that they had not been told when it was kept (criterion 4);
4. estate-wide, nothing parked on the DLQ stream from the tenant's creation on, and both of the catalog's outboxes are
   drained.

These are the readings XC-090's closes-when names versions_below_tip, unknown_to_graph, the dead-lettered delta and the
outbox depth, taken where provenance and events are kept (ADR 0158): the commits' `rask.*` properties, the Lance-stored
feed and edge list, the DLQ stream and the outboxes. Each is present before it is compared, because the kept drive
committed versions that name an input, was announced, and told each person something: a soak that reads an emptied
tenant fails rather than passing on nothing.

WHAT IS NOT CLAIMED, and why. Only the drive's two tables are read: criterion 5's drill tables commit enough versions for
maintenance to expire old ones, and criterion 2's objects are its own. An inbox keeps only its newest rows
(`RASK_NOTIFICATIONS_INBOX_MAX_ROWS`), so a row the drive put in a busy one may have been trimmed by the time the soak
reads it: claim 3 holds what each person is told now to what they were told then, and not the other way.

The estate runs `LANCE_E2E_SOAK_SECONDS` from the moment the drive was kept, a day unless the run says otherwise. The
e2e-stack lane soaks for no time at all, so keeping and resuming a drive runs on every push; the deployed estate soaks for
the day: `bash scripts/e2e_live.sh -m phase1 --require-live --chaos --keep-drive PATH`, then a day later
`bash scripts/e2e_live.sh -m soak --require-live --soak PATH`, which removes the kept tenant once the soak passes.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bus
import byo_scenario as scenario
import kept_drive
import pytest
from byo_scenario import Tenant
from correctness import announced, as_named, edges_named, history_of, recorded_edges, settled_control_outbox_depth, settled_outbox_depth, told
from kept_drive import KeptDrive


#: How long the estate runs from the moment the drive was kept before the soak reads it: a day unless the run says otherwise.
SOAK_SECONDS = float(os.environ.get("LANCE_E2E_SOAK_SECONDS", str(24 * 60 * 60)))

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.soak,
    pytest.mark.skipif(not (scenario.LIVE and bus.NATS), reason=f"{scenario.REQUIRES}, and LANCE_E2E_NATS_URL (the bus)"),
]


@pytest.fixture(scope="module")
def kept(request: pytest.FixtureRequest) -> Iterator[tuple[Tenant, KeptDrive]]:
    """The drive kept at `--soak`, and its tenant signed in again, once the estate has run `SOAK_SECONDS` since. The
    tenant is removed once every test here has passed, and kept for a reader of what failed otherwise."""
    path = Path(str(request.config.getoption("--soak")))
    tenant, drive = kept_drive.resume(path)
    soaked = datetime.now(UTC) - drive.kept_at
    wanted = timedelta(seconds=SOAK_SECONDS)
    assert soaked >= wanted, f"the drive kept at {path} has soaked {soaked} of {wanted}: read it after {drive.kept_at + wanted:%Y-%m-%d %H:%M} UTC"
    assert kept_drive.principals(tenant) == drive.principals, "the people signed in now are not the ones who played the drive's parts"
    yield tenant, drive
    if kept_drive.passed_whole(request.session):
        left = tenant.tear_down()
        assert not left, f"the kept tenant {tenant.project} was not fully removed: {left}"


def test_the_estate_left_running_still_reads_the_drive_as_the_criteria_did(kept: tuple[Tenant, KeptDrive]) -> None:
    """One reading of every surface, then every finding at once: a soak takes a day to repeat, so it reports all that
    differ, each by the claim it breaks."""
    tenant, drive = kept
    tables = (drive.run.bronze_table, drive.run.silver_table)
    history = history_of(tenant.pipeline, tables)
    committed = as_named(drive.run.commits, author=drive.principals["pipeline"])
    owed = edges_named(history)
    edges = recorded_edges(tenant.admin, tables, expected=owed)
    feed = announced(tenant.pipeline, drive.run)
    heard = told(tenant, drive.run)
    parked = bus.messages_since("DLQ", drive.created_at)
    depths = {"lineage": settled_outbox_depth(tenant.admin), "control": settled_control_outbox_depth()}
    named = {(row.table, row.ref, row.version) for row in feed}
    findings = {
        "versions Lance holds other than as the drive committed them": [
            version for version in sorted(history.keys() | committed.keys(), key=repr) if history.get(version) != committed.get(version)
        ],
        "edges recorded beyond those the versions name": list((Counter(edges) - Counter(owed)).elements()),
        "edges the versions name and the list lacks": list((Counter(owed) - Counter(edges)).elements()),
        "versions no feed row names": [version for version in sorted(history, key=repr) if version not in named],
        "feed rows recorded since the drive was kept": [row for row in feed if row not in drive.feed],
        "feed rows gone since the drive was kept": [row for row in drive.feed if row not in feed],
        "told again": [(part, row) for part, rows in heard.items() for row in (Counter(rows) - Counter(drive.told[part])).elements()],
        "parked on the DLQ since the tenant was made": [(message.seq, message.subject) for message in parked],
        "left in an outbox": [f"{outbox}: {depth}" for outbox, depth in depths.items() if depth],
    }

    assert owed and drive.feed and all(drive.told.values()), (
        f"the kept drive named no input, held no feed row, or told somebody nothing, so there is nothing to soak: {drive.told}"
    )
    assert findings == {finding: [] for finding in findings}
