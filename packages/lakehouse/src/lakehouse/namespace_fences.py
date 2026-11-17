"""Who may change a namespace's rows right now: something added beneath it, or its drop (ADR 0233).

THE `dir` BACKEND JUDGES EACH AGAINST THE `__manifest` IT READ. A create, declare, register or rename checks that the new
row's parent stands, and a Restrict drop that the namespace holds no row, each against the version of ``__manifest`` it
read, and ``lance_docs/ns_catalog/catalog/dir/index.md`` § Manifest Table Commits promises a conflict only between two
writes of one entry. Measured on pylance 13.0.0: an add sent beside its parent's Restrict drop and the drop both landed
(50 of 50 races of a namespace, 30 of 30 of a declared table), and the child stood under no parent. Sent one after the
other, from two connections, each read the other's commit and refused (``NamespaceNotFound`` for the add,
``NamespaceNotEmpty`` for the drop).

SO THE TWO NEVER OVERLAP. Each holds the fence of the namespace they meet at first (:class:`NamespaceFence`): an add the
fence of the namespace it adds into, a drop the fence of the namespace it drops. Adds share it, and drops share it; an add
and a drop refuse each other (:class:`NamespaceFencedError`, answered 409 as the spec's ConcurrentModification). One
fence per namespace NAME, not uuid: it fences the row a name holds in ``__manifest``, whichever namespace that is.

ADDS NEVER REFUSE ADDS. Every create beneath a namespace takes its fence, so the fence keeps one object per holder
(:class:`lakehouse.fences.HolderFence`): an add writes its own and never contends with another add for a write. Measured
on RustFS 1.0.3 with one shared record: 8 of 16, and 39 of 64, concurrent creates into one namespace answered 409 with
no drop anywhere, each take having lost its eight rounds to the other adds (ADR 0233).

The same fence holds a namespace's other writes against its drop: the namespace undrop holds it for an add while it
restores the subtree, a recoverable cascade and the warehouse delete hold it for a drop for their whole run, and a trash
record is filed under a warehouse only while its fence is held for an add (:func:`lakehouse.trash.put`), so the warehouse
delete, which reads its trash once, cannot miss one.

The holding is :class:`lakehouse.fences.HolderFence`'s. Every method is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from enum import StrEnum
from typing import ClassVar, Final

from lance_namespace import ConcurrentModificationError

from lakehouse import control_prefixes
from lakehouse.fences import HolderFence, HolderRecord


PREFIX: Final = control_prefixes.NAMESPACE_FENCES

#: How long a take, and each renewal of it, holds the fence. An add or a drop takes seconds; a cascade or a warehouse
#: delete renews its hold while it runs. A holder that crashed keeps adds, or drops, off the name for at most this long.
HOLD: Final = timedelta(minutes=2)


class NamespaceWork(StrEnum):
    """What a holder does to the namespace."""

    #: Puts something beneath it: a child namespace or a table (a create, declare, register, rename in or undrop), a
    #: trash record under a warehouse, or a namespace undrop's restore.
    ADD = "add"
    #: Drops it: its Restrict drop, a cascade of it, or a warehouse's delete.
    DROP = "drop"


class NamespaceFencedError(ConcurrentModificationError):
    """The namespace is held by work that excludes this take's: the spec's ConcurrentModification, which a door answers 409
    and its caller retries."""


class NamespaceHolder(HolderRecord):
    """One holder's object, naming the namespace it fences."""

    name: str


def fence_directory(name: str) -> str:
    """The key of the directory of the fence of the namespace named ``name`` (the canonical ``$``-joined id), under a digest
    of the name: one object per holder beneath it."""
    return f"{PREFIX}/namespace-{hashlib.sha256(name.encode()).hexdigest()[:32]}"


class NamespaceFence(HolderFence):
    """Where one namespace name's fence is kept: the control root, the credential that writes there, and the name."""

    log_name: ClassVar[str] = "namespace_fence"
    stored_field: ClassVar[str] = "name"
    holder_model: ClassVar[type[HolderRecord]] = NamespaceHolder
    kinds: ClassVar[frozenset[str]] = frozenset(NamespaceWork)

    name: str

    @property
    def directory(self) -> str:
        return fence_directory(self.name)

    @property
    def subject(self) -> str:
        return self.name

    @property
    def what(self) -> str:
        return f"namespace {self.name!r}"

    def hold(self) -> timedelta:
        return HOLD

    def excludes(self, held: str, work: str) -> bool:
        return held != work

    def refused(self, message: str) -> Exception:
        return NamespaceFencedError(message)
