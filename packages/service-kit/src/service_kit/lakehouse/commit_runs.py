"""The catalog's own record of what each caller's run committed through ``/commit`` ([[LH-280]]).

A run id is the caller's word, and any writer of a table can put it on a commit, so a commit's
transaction properties cannot say which run made it. A commit that brings its fragments is recognized
by those fragments; this record answers the one question the data cannot, "what did this run commit?"
asked with nothing to commit, as a committer asks after it has discarded the fragments it committed.

The door writes it through the control-root credential, which no vend reaches (register's location
exclusivity, [[LH-279]]), and keys it by the door's verified subject, so another identity naming the same
run id answers only itself. A reader trusts it only while the version it names still exists.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from service_kit.lakehouse.base_refs import decoded_path, normalise
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import get_record, put_record, record_key


log = logging.getLogger(__name__)

_PREFIX = "_commit_runs"


class CommitRun(BaseModel):
    """Who is committing which run, and the control root the catalog records it on."""

    model_config = ConfigDict(frozen=True)

    control_root: str
    storage_options: StorageOptions = Field(default_factory=dict)
    #: The door's verified caller.
    subject: str
    run_id: str


class CommittedRun(BaseModel):
    """The version one subject's run last committed to the table rooted at ``location``."""

    model_config = ConfigDict(frozen=True)

    location: str
    subject: str
    run_id: str
    version: int


def _key(location: str, run: CommitRun) -> str:
    # A JSON array, not a separator: a JSON-decoded run id may hold any character, and the encoding is injective.
    return record_key(_PREFIX, "run", json.dumps([decoded_path(location), run.subject, run.run_id]))


def record_committed(run: CommitRun, location: str, *, version: int) -> None:
    """Record that ``run`` committed ``version`` to the table rooted at ``location``; the newest commit wins.

    Raises:
        OSError: The control root cannot be written.
    """
    record = CommittedRun(location=normalise(location), subject=run.subject, run_id=run.run_id, version=version)
    put_record(run.control_root, run.storage_options, _key(location, run), record.model_dump(mode="json"))


def read_committed(run: CommitRun, location: str) -> CommittedRun | None:
    """What ``run`` last committed to the table rooted at ``location``, or ``None`` when nothing is recorded.

    A stored record that is malformed, or names another table, subject or run, reads as ``None`` with a
    warning: the caller answers "no prior commit", as for a record never written.

    Raises:
        OSError: The control root cannot be read.
    """
    raw = get_record(run.control_root, run.storage_options, _key(location, run), event="commit_run_record")
    if raw is None:
        return None
    try:
        record = CommittedRun.model_validate(raw)
    except ValidationError as exc:
        log.warning("commit_run_record_malformed", extra={"location": normalise(location), "run_id": run.run_id, "error": str(exc)})
        return None
    if (decoded_path(record.location), record.subject, record.run_id) != (decoded_path(location), run.subject, run.run_id):
        log.warning("commit_run_record_mismatched", extra={"location": normalise(location), "run_id": run.run_id, "record": record.location})
        return None
    return record
