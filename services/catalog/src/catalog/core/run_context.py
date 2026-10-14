"""The run a governed write names: its id, its job and the inputs it read (ADR 0156 C2, ADR 0157, [[LH-225]]).

Provenance travels in the spec ``Context`` (``lance_docs/ns_catalog/spec.yaml`` § Context): a client's
``header.<name>`` entry reaches the catalog as the request header ``<name>``. On its own write doors the catalog
reads the run from those headers and stamps it into the commit as Lance transaction properties; a stock client
committing through ``version/create`` writes the same properties into its staged manifest itself, and the catalog
reads them there. Either way the committed manifest carries the run, so a crash after the commit loses nothing a
reader of the version's transaction cannot recover.

``rask.author`` names who committed: the verified caller, which the catalog stamps beside the run on its own doors and
requires of a stock client's run at ``version/create``. A property any committer can write proves who wrote it only
because the catalog refuses every other value before it finalizes.

The properties are the only ``rask.*`` keys a commit may carry. ``rask.inputs`` is a JSON array of
``{"table": [<id segment>, ...], "version": <n>, "branch": <name>}``, ``branch`` omitted for main: the table is the
spec identifier as a list, so it reads the same whatever delimiter the catalog is configured with.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Final

from lance_namespace import InvalidInputError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError


RUN_ID_HEADER: Final = "x-rask-run-id"
JOB_HEADER: Final = "x-rask-job"
INPUTS_HEADER: Final = "x-rask-inputs"

RUN_ID_PROPERTY: Final = "rask.run_id"
JOB_PROPERTY: Final = "rask.job"
INPUTS_PROPERTY: Final = "rask.inputs"
AUTHOR_PROPERTY: Final = "rask.author"

#: Every ``rask.*`` transaction property a commit may carry.
PROPERTIES: Final = frozenset({RUN_ID_PROPERTY, JOB_PROPERTY, INPUTS_PROPERTY, AUTHOR_PROPERTY})

#: Each input is one FGA check on the commit path, so the list is bounded.
MAX_INPUTS: Final = 64

type _Name = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")]


class RunInput(BaseModel):
    """One table the run read, at the version it read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: Annotated[list[_Name], Field(min_length=1)]
    version: Annotated[int, Field(ge=1)]
    branch: _Name | None = None


class RunContext(BaseModel):
    """The run behind one commit."""

    model_config = ConfigDict(frozen=True)

    run_id: _Name
    job: _Name | None = None
    inputs: Annotated[tuple[RunInput, ...], Field(max_length=MAX_INPUTS)] = ()

    def properties(self, *, author: str | None) -> dict[str, str]:
        """The ``rask.*`` transaction properties that carry this run in a commit made by ``author``."""
        stamped = {RUN_ID_PROPERTY: self.run_id}
        if author is not None:
            stamped[AUTHOR_PROPERTY] = author
        if self.job is not None:
            stamped[JOB_PROPERTY] = self.job
        if self.inputs:
            stamped[INPUTS_PROPERTY] = json.dumps([entry.model_dump(exclude_none=True) for entry in self.inputs], separators=(",", ":"))
        return stamped


def _parse(run_id: str | None, job: str | None, inputs: str | None, *, where: str) -> RunContext | None:
    if run_id is None:
        if job is not None or inputs is not None:
            raise InvalidInputError(f"{where} names a job or inputs but no run id")
        return None
    try:
        listed = json.loads(inputs) if inputs is not None else []
    except (ValueError, RecursionError) as exc:
        raise InvalidInputError(f"{where}: the inputs are not JSON") from exc
    if not isinstance(listed, list):
        raise InvalidInputError(f"{where}: the inputs are a JSON array of {{table, version, branch}}")
    try:
        return RunContext.model_validate({"run_id": run_id, "job": job, "inputs": listed})
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        field = ".".join(str(part) for part in first["loc"])
        raise InvalidInputError(f"{where}: {field}: {first['msg']}") from exc


def from_headers(headers: Mapping[str, str]) -> RunContext | None:
    """The run a request's Context names, or ``None`` when it names none.

    Raises:
        InvalidInputError: The headers name a job or inputs without a run id, or one of them is malformed.
    """
    return _parse(headers.get(RUN_ID_HEADER), headers.get(JOB_HEADER), headers.get(INPUTS_HEADER), where="the request's Context")


def from_properties(properties: Mapping[str, str] | None) -> RunContext | None:
    """The run a commit's transaction properties name, or ``None`` when they name none.

    Raises:
        InvalidInputError: A ``rask.*`` key other than the run's own is present, a job or inputs are named without a
            run id, or one of them is malformed.
    """
    properties = properties or {}
    foreign = sorted(key for key in properties if key.startswith("rask.") and key not in PROPERTIES)
    if foreign:
        raise InvalidInputError(f"commit refused: the rask.* transaction properties a commit may carry are {sorted(PROPERTIES)}, not {foreign}")
    return _parse(properties.get(RUN_ID_PROPERTY), properties.get(JOB_PROPERTY), properties.get(INPUTS_PROPERTY), where="the commit's transaction properties")


def commit_properties(run: RunContext | None, *, author: str | None) -> dict[str, str] | None:
    """The transaction properties a catalog door stamps for ``run``, committed by ``author``; ``None`` without a run."""
    return run.properties(author=author) if run is not None else None


def require_caller_authored(properties: Mapping[str, str], run: RunContext | None, *, caller: str | None) -> None:
    """Refuse a staged commit whose ``rask.author`` is not the verified ``caller``, or whose run names no author.

    ``caller`` is ``None`` when the catalog authenticates nobody, and then there is no one to compare with.

    Raises:
        InvalidInputError: The commit names an author other than the caller, or a run without its author.
    """
    if caller is None:
        return
    author = properties.get(AUTHOR_PROPERTY)
    if author is not None and author != caller:
        raise InvalidInputError(f"commit refused: {AUTHOR_PROPERTY} names {author!r}, but the commit is {caller!r}'s; it names the verified caller")
    if author is None and run is not None:
        raise InvalidInputError(f"commit refused: a commit naming its run names its author too, {AUTHOR_PROPERTY}={caller!r}")
