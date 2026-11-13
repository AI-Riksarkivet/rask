"""What git declares of the estate's tenants, and the record of the tuples written from it ([[XC-213]], ADR 0206).

The chart renders its ``catalog.declared`` values into a file the catalog mounts: the projects, the warehouses, the
grants to principals that are not people, and the estate's own grants (its administrators, ``auth.bootstrapAdmin``, and
the roles the release's services hold on its root, [[XC-011]]). The catalog converges the estate onto it (``catalog.api.declaration_converge``),
so an install from git holds its tenants with no call to any door. People's grants, namespaces and tables are not
declared: they stay with their doors.

A project or warehouse the declaration names carries ``managed_by: git`` on its record, and its doors refuse a change to
what git declares of it. A tuple written from the declaration is recorded under ``_declared_grants/`` before it is
written, one record per tuple, so the tuple git stops declaring is revoked and a grant of the same shape that somebody
made at a door, which no record names, is never taken.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from catalog.schemas import DeleteProfile
from lakehouse import control_prefixes
from lakehouse.governed import fga
from lakehouse.governed.principal import PrincipalKey
from lakehouse.governed.subjects import is_person_subject
from lakehouse.naming import CONTROL_ID_PATTERN, CONTROL_ID_RE
from lakehouse.record_store import delete_record, list_records
from lakehouse.records import RecordExistsError, create_json
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: The object types a declared grant may name: the two the declaration itself makes.
_GRANTED_TYPES: Final = ("project", "warehouse")

#: A project, warehouse or bucket id. pydantic's regex engine anchors ``$`` at the end of the text alone.
type ControlId = Annotated[str, StringConstraints(pattern=rf"^{CONTROL_ID_PATTERN}$")]


class _Declared(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeclaredProject(_Declared):
    """A tenant git declares: its id, the people who administer it, and its deletion protection."""

    id: ControlId
    admins: list[PrincipalKey] = Field(default_factory=list)
    protected: bool = False


class DeclaredWarehouse(_Declared):
    """A warehouse git declares. ``bucket`` defaults to the id, as at the create door."""

    id: ControlId
    project: ControlId
    bucket: ControlId | None = None
    delete_profile: DeleteProfile | None = None
    protected: bool = False
    active: bool = True


class DeclaredGrant(_Declared):
    """A rung on a project or warehouse, granted to a principal that is not a person."""

    object: str
    relation: str
    principal: PrincipalKey

    @model_validator(mode="after")
    def _a_machine_on_a_declared_object(self) -> Self:
        fga_type, _, object_id = self.object.partition(":")
        if fga_type not in _GRANTED_TYPES or not CONTROL_ID_RE.fullmatch(object_id):
            raise ValueError(f"a declared grant names a project or a warehouse (`project:<id>`, `warehouse:<id>`), not {self.object!r}")
        if is_person_subject(self.principal):
            raise ValueError(f"{self.principal!r} is a person: a person's grant is made at the catalog's access doors, never declared")
        if not fga.admits(fga_type, self.relation, fga.user_object(self.principal)):
            raise ValueError(f"{self.relation!r} is not a rung a principal is granted on a {fga_type}")
        return self

    @property
    def grant(self) -> GrantTuple:
        """The tuple this grant asks the store to hold."""
        return GrantTuple(user=fga.user_object(self.principal), relation=self.relation, object=self.object)


class GrantTuple(BaseModel):
    """One OpenFGA tuple the declaration asks for, or the record of one written from it."""

    model_config = ConfigDict(frozen=True)

    user: str
    relation: str
    object: str

    @property
    def client_tuple(self) -> fga.ClientTuple:
        """This tuple as the OpenFGA client writes and deletes it."""
        return fga.ClientTuple(user=self.user, relation=self.relation, object=self.object)


class DeclaredEstateRole(_Declared):
    """A machine role on the estate root, held by one of the release's own services."""

    relation: str
    principal: PrincipalKey

    @model_validator(mode="after")
    def _a_machine_role(self) -> Self:
        if self.relation not in fga.machine_relations("estate"):
            raise ValueError(f"{self.relation!r} is not a machine role on the estate root")
        if is_person_subject(self.principal):
            raise ValueError(f"{self.principal!r} is a person: a machine role names a service's own account")
        return self


class DeclaredEstate(_Declared):
    """The estate's own grants: the people who administer it and the roles the release's services hold on its root.

    An administrator holds ``admin`` on the estate root and ``owner`` of the estate's own warehouse, the grant every
    other one in the estate starts from, so it is declared rather than written by anyone: changing it in git moves it.
    """

    admins: list[PrincipalKey] = Field(default_factory=list)
    roles: list[DeclaredEstateRole] = Field(default_factory=list)

    @model_validator(mode="after")
    def _people_administer(self) -> Self:
        if machines := sorted(admin for admin in self.admins if not is_person_subject(admin)):
            raise ValueError(f"{', '.join(machines)} administer nothing: the estate's administrator is a person, and a service holds a role")
        return self


class Declaration(_Declared):
    """Everything git declares of the estate and its tenants."""

    projects: list[DeclaredProject] = Field(default_factory=list)
    warehouses: list[DeclaredWarehouse] = Field(default_factory=list)
    grants: list[DeclaredGrant] = Field(default_factory=list)
    estate: DeclaredEstate = Field(default_factory=DeclaredEstate)

    @model_validator(mode="after")
    def _each_id_once(self) -> Self:
        for kind, ids in (("project", [p.id for p in self.projects]), ("warehouse", [w.id for w in self.warehouses])):
            if repeated := sorted({i for i in ids if ids.count(i) > 1}):
                raise ValueError(f"{kind} declared more than once: {', '.join(repeated)}")
        return self

    def tuples(self, *, root: str, estate_warehouse: str) -> set[GrantTuple]:
        """Every tuple the declaration asks for: each project's admins, each grant, and the estate's own.

        Args:
            root: The estate root's object (``RASK_FGA_ROOT_OBJECT``).
            estate_warehouse: The estate's own warehouse's object (``RASK_FGA_DEFAULT_WAREHOUSE_OBJECT``).
        """
        admins = {
            GrantTuple(user=fga.user_object(admin), relation="admin", object=f"project:{project.id}") for project in self.projects for admin in project.admins
        }
        estate = {
            GrantTuple(user=fga.user_object(admin), relation=relation, object=obj)
            for admin in self.estate.admins
            for relation, obj in (("admin", root), ("owner", estate_warehouse))
        }
        roles = {GrantTuple(user=fga.user_object(role.principal), relation=role.relation, object=root) for role in self.estate.roles}
        return admins | {declared.grant for declared in self.grants} | estate | roles


class DeclarationUnreadableError(ValueError):
    """The mounted declaration is absent or is not one: nothing is converged from it, and nothing it named is released."""


def load(path: str) -> Declaration:
    """The declaration mounted at ``path``.

    Raises:
        DeclarationUnreadableError: The file cannot be read, is not JSON, or does not declare what a declaration may.
    """
    try:
        return Declaration.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise DeclarationUnreadableError(f"the declaration at {path!r} cannot be read as one: {exc}") from exc


def _grant_key(grant: GrantTuple) -> str:
    digest = hashlib.sha256(json.dumps([grant.user, grant.relation, grant.object]).encode()).hexdigest()
    return f"{control_prefixes.DECLARED_GRANTS}/{digest}.json"


def recorded_grants(control_root: str, storage_options: StorageOptions) -> set[GrantTuple]:
    """The tuples the catalog wrote from a declaration and has not revoked.

    Strict: a record skipped would be a tuple git no longer declares that is never revoked.

    Raises:
        OSError: The store failed a read.
        ValueError: A record is there and cannot be read as one.
    """
    records = list_records(control_root, storage_options, control_prefixes.DECLARED_GRANTS, event="declared_grant", strict=True)
    return {GrantTuple.model_validate(record) for record in records}


def record_grant(control_root: str, storage_options: StorageOptions, grant: GrantTuple) -> None:
    """Record ``grant`` as the declaration's, before its tuple is written; one already recorded stays as it is."""
    try:
        create_json(control_root, storage_options, _grant_key(grant), grant.model_dump())
    except RecordExistsError:
        return


def forget_grant(control_root: str, storage_options: StorageOptions, grant: GrantTuple) -> None:
    """Remove the record of ``grant``, once its tuple is revoked."""
    delete_record(control_root, storage_options, _grant_key(grant))
