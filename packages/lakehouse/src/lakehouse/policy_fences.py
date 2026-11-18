"""Who may change the maintenance policy of a namespace or a project right now: a reclaim of a table it governs, or a write
of the policy itself (ADR 0243 §6).

A NAMESPACE'S OR A PROJECT'S POLICY GOVERNS EVERY TABLE BENEATH IT. A hold set there (``cleanup_enabled`` false) is how an
operator freezes many tables at once, and a maintenance pass reads the policy that governs its table before its cleanup
and once more under its holds (``maintenance.services.expiry._policy_changed``). A write landing after that read, while
the cleanup deletes, answered 200 while versions it keeps were deleted (reproduced on moto and RustFS 1.0.3 by review:
versions [1, 2, 3] became [3] under a namespace's hold).

SO THE TWO NEVER OVERLAP ON ONE LEVEL. Every pass that deletes holds, for as long as it deletes, the policy fence of each
level above its table that can govern it, each namespace of its chain and its project (:class:`PolicyFence`), as
:attr:`PolicyWork.RECLAIM`, which passes share; a door that writes or deletes the policy of a namespace or a project holds
that level's fence as :attr:`PolicyWork.SET`, which excludes every reclaim, and confirms its hold just before its write
(:meth:`lakehouse.fences.Fence.write_held`). Of a pass and a door one sees the other: the door answers 409 while a pass
deletes beneath it (:class:`PolicyFencedError`, the spec's ConcurrentModification, retryable), and a pass that finds the
door's hold deletes nothing and is passed again. A table's own policy is fenced by its history
(:mod:`lakehouse.history_fences`), which every pass of it already holds.

A maintain-tier vend reaches the fence directories of the namespaces of its own table's warehouse and of its own
project, and of each only the keys shaped as a reclaim's holder (``catalog.core.vending.TableRecords``): not exactly its
own levels, since one grant per level does not fit an STS session policy's 2,048 characters beyond a chain of two or three
(measured, ADR 0243 §6), so every namespace fence of a warehouse lies beneath one directory (:func:`warehouse_directory`).
What it can plant there is at most a reclaim's hold, for one hold, on a namespace of its own warehouse or on its own
project, which delays that level's policy writes and nothing else: never a policy, never a door's hold, never another
warehouse's or project's; a take judges only the names the fence defines, directly in its directory, a holder's kind by
its key (:class:`lakehouse.fences.HolderFence`).

The holding, renewing and releasing are :class:`lakehouse.fences.HolderFence`'s. Every method is BLOCKING IO; callers
threadpool it.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from enum import StrEnum
from typing import ClassVar, Final, Literal

from lance_namespace import ConcurrentModificationError

from lakehouse import control_prefixes, history_fences
from lakehouse.fences import HolderFence, HolderRecord
from lakehouse.storage_options import StorageOptions


PREFIX: Final = control_prefixes.POLICY_FENCES

#: How long a take, and each renewal of it, holds the fence: the history fence's, so a pass's maintain-tier credential,
#: vended for at most that long (ADR 0243 A1), dies no later than any hold it signs lapses.
HOLD: Final = history_fences.HOLD

#: The levels above a table whose policy can govern it (``maintenance_policies.governing_policy``).
type PolicyLevel = Literal["namespace", "project"]


class PolicyWork(StrEnum):
    """What a holder does to the level's policy."""

    #: Deletes versions of a table the level's policy governs: a maintenance pass, or a catalog reclaim door.
    RECLAIM = "reclaim"
    #: Writes or deletes the level's policy.
    SET = "set"


class PolicyFencedError(ConcurrentModificationError):
    """The level's policy is held by work that excludes this take's: the spec's ConcurrentModification, which a door answers
    409 and its caller retries."""


class PolicyHolder(HolderRecord):
    """One holder's object, naming the level it fences as ``<level>:<key>``."""

    level: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def warehouse_directory(warehouse: str) -> str:
    """The key of the directory every namespace fence of the warehouse ``warehouse`` (a table name's first segment) lies in:
    what one maintain-tier vend is granted for all of its table's namespaces, at any depth, in two strings (an STS session
    policy holds 2,048 characters, ``catalog.core.vending.SESSION_POLICY_LIMIT``, and one grant per level does not fit a
    chain of three under a long bucket name)."""
    return f"{PREFIX}/w{_digest(warehouse)}"


def fence_directory(level: PolicyLevel, key: str, *, warehouse: str | None = None) -> str:
    """The key of the directory of the policy fence of the ``level`` keyed ``key`` (a namespace's uuid, beneath its
    ``warehouse``'s directory, or a project's id), under digests: one object per holder beneath it, and a name no IAM
    policy syntax can enter.

    Raises:
        ValueError: A namespace's fence named no warehouse.
    """
    if level == "project":
        return f"{PREFIX}/p{_digest(key)}"
    if warehouse is None:
        raise ValueError(f"the policy fence of namespace {key!r} lies under its warehouse's directory, and none was named")
    return f"{warehouse_directory(warehouse)}/n{_digest(key)}"


def levels(control_root: str, storage_options: StorageOptions, *, warehouse: str, namespace_uuids: list[str], project: str | None) -> list[PolicyFence]:
    """The policy fence of every level above a table whose policy can govern it, each namespace of its chain (beneath its
    ``warehouse``, its name's first segment) and its ``project``, on ``control_root`` under ``storage_options``: the one
    place both a maintenance pass and the catalog's reclaim and policy doors find them, each naming the control root
    its settings render from the chart's one ``objectStore.bucket``."""
    fences = [
        PolicyFence(control_root=control_root, storage_options=storage_options, level="namespace", key=namespace_uuid, warehouse=warehouse)
        for namespace_uuid in namespace_uuids
    ]
    if project is not None:
        fences.append(PolicyFence(control_root=control_root, storage_options=storage_options, level="project", key=project))
    return fences


class PolicyFence(HolderFence):
    """Where one level's policy fence is kept: the control root, the credential that writes there, and the level."""

    log_name: ClassVar[str] = "policy_fence"
    stored_field: ClassVar[str] = "level"
    holder_model: ClassVar[type[HolderRecord]] = PolicyHolder
    kinds: ClassVar[frozenset[str]] = frozenset(PolicyWork)

    level: PolicyLevel
    key: str
    #: The warehouse a namespace lies in, its name's first segment; ``None`` for a project.
    warehouse: str | None = None

    @property
    def directory(self) -> str:
        return fence_directory(self.level, self.key, warehouse=self.warehouse)

    @property
    def subject(self) -> str:
        return f"{self.level}:{self.key}"

    @property
    def what(self) -> str:
        return f"the {self.level} {self.key!r}'s maintenance policy"

    def hold(self) -> timedelta:
        return HOLD

    def excludes(self, held: str, work: str) -> bool:
        return held != work

    def refused(self, message: str) -> Exception:
        return PolicyFencedError(message)
