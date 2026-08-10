"""The inbox actor — one virtual actor per SUBJECT.

Dapr routes `InboxActor/<base64url(sub)>` to exactly one pod cluster-wide and serialises calls to it,
so **turn-based concurrency IS the lock**. Two tabs marking rows read, a bus delivery and a compaction
tick cannot interleave, and there is deliberately **no etag round-trip and no optimistic concurrency
anywhere in this file**. Adding OCC here would be machinery for a race the runtime already removed —
and worse than useless, because the unread count is *derived* from the rows on every mutation, so
there is no second truth for a lost write to desynchronise.

**Identity is derived, never accepted.** No method here takes a subject. The actor id IS
`encode_subject(verified_sub)`, minted by the HTTP layer from the token, and this actor reads its own
subject back out of it — so there is no parameter anyone could forget to check. The stored records
carry their `subject` as a SECOND lock: a record whose owner disagrees with the id is *unreadable*,
not *absent* (`InboxUnreadable`), because reporting it as absent would invite the caller to overwrite
somebody else's rows.

**Authorization is NOT performed here.** The HTTP layer checks FGA before it invokes, exactly as
`services/annotator/src/annotator/projects/actor.py` pins for the task plane. Keeping the split means
this file is testable with no OpenFGA, and the op→privilege map has one home.

**Two stores, one actor, no transaction between them.** Reminders do not live in the actor state
store: this estate deploys `dapr-scheduler-server` at 3 replicas on its own etcd PVC, so a
reminder-carrying actor spans the Scheduler's etcd AND `lance-statestore`'s Postgres, and either can
be lost or rolled back independently of the other. That has two consequences here, and both are load-
bearing rather than stylistic:

1. The compaction reminder is armed **before** the rows are persisted and disarmed **after** — the
   ordering `19677ff` fixed in the project actor, protecting a real split rather than a preference.
2. A lost reminder must be **recoverable**, so every interaction re-arms an overdue one from the READ
   path. Compose that with the fact that `ActorStateTTL` is off on this estate (open_notifications
   §11 q6, read from the chart): the compaction reminder is the *only* authoritative bound on an
   inbox, and armed-once-and-trusted would leave it with no floor at all. The `ttlInSeconds` belt is
   written only where the sidecar will accept it — daprd REJECTS the whole state transaction when that
   metadata arrives with the feature off, so "write it anyway, it's free" is the one thing this file
   must not do. `NotificationsSettings.actor_state_ttl_enabled` gates it and defaults to the chart's
   actual answer; nothing in this design depends on the TTL either way.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, override

from dapr.actor import Actor, ActorInterface, Remindable, actormethod
from pydantic import BaseModel, ValidationError

from notifications.config import get_notifications_settings
from notifications.errors import InboxUnreadable
from notifications.feed import compact, paginate, unread_count
from notifications.models import (
    InboxDismiss,
    InboxMark,
    InboxMeta,
    InboxPage,
    InboxPointer,
    InboxQuery,
    InboxRows,
    InboxWatches,
    NotificationDelivery,
)
from service_kit.governed.user_state import decode_subject


logger = logging.getLogger(__name__)


#: The actor type as the sidecar routes it. Dapr derives it from the class name; naming it here gives
#: the proxy one place to read it from (`tests/test_inbox_actor.py` pins the two together).
INBOX_ACTOR_TYPE = "InboxActor"

#: The two state partitions — "small and read every call" vs "large and read rarely", the split
#: `annotator.projects.actor` already uses for a task and its draft. The badge is by far the most
#: frequent read in this plane and it never touches the rows.
META_KEY = "inbox-meta"
ROWS_KEY = "inbox-rows"
#: The THIRD partition: this subject's watch list. Read only by the settings surface and by a fan-out
#: resolving one subject — never by the badge, which is why it is not folded into the meta record.
WATCHES_KEY = "inbox-watches"

#: The compaction reminder's name. One per actor; repeating until the inbox is empty.
COMPACTION_REMINDER = "compaction"


def _parse[T: BaseModel](partition: str, raw: str, model: type[T]) -> T:
    """Decode a stored record, or refuse it. Schema drift is UNREADABLE, never absent."""
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        logger.exception("inbox_record_unreadable", extra={"partition": partition})
        raise InboxUnreadable(partition, "the stored record no longer fits its schema") from exc


def _require_owner(partition: str, stored: str, subject: str) -> None:
    """The second lock: the record's own `subject` against the one derived from the actor id.

    Redundant by construction, which is exactly why it is worth checking — the only way the two can
    disagree is a mis-keyed or hand-edited row, and serving that would be the cross-subject leak this
    plane must make impossible. Refusing it as unreadable (rather than absent) also stops THIS caller
    overwriting the other subject's rows on their next write.
    """
    if stored != subject:
        logger.error("inbox_owner_mismatch", extra={"partition": partition, "stored_subject": stored})
        raise InboxUnreadable(partition, "the stored record belongs to another subject")


class InboxActorInterface(ActorInterface):
    """The wire surface. The names are the ids the sidecar routes on, so they are fixed here rather
    than derived from the Python method names — renaming one breaks any in-flight invocation.

    The bodies raise rather than being `...`: they never execute (the concrete actor overrides every
    one, and `@actormethod` only registers the name), but a declaration still has to satisfy its own
    return type or `ty` reports it as always returning None.
    """

    @actormethod(name="Deliver")
    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="Page")
    async def page(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="Unread")
    async def unread(self) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="MarkSeen")
    async def mark_seen(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="Dismiss")
    async def dismiss(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="GetWatches")
    async def get_watches(self) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="SetWatch")
    async def set_watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class InboxActor(Actor, InboxActorInterface, Remindable):
    """Holds one subject's pointer rows and their read state."""

    def _subject(self) -> str:
        """This actor's subject, decoded from its own id.

        The id is `encode_subject(sub)` — base64url with padding stripped, because Dapr reserves `||`
        as its app-id separator and the key travels in the sidecar's URL path, where percent-encoding
        would be decoded back into the reserved sequence.
        """
        try:
            return decode_subject(self.id.id)
        except (ValueError, UnicodeDecodeError) as exc:
            raise InboxUnreadable("actor-id", "the actor id is not an encoded subject") from exc

    async def _read_meta(self) -> InboxMeta | None:
        has, raw = await self._state_manager.try_get_state(META_KEY)
        if not has or not raw:
            return None
        meta = _parse(META_KEY, raw, InboxMeta)
        _require_owner(META_KEY, meta.subject, self._subject())
        return meta

    async def _read_rows(self) -> InboxRows | None:
        has, raw = await self._state_manager.try_get_state(ROWS_KEY)
        if not has or not raw:
            return None
        rows = _parse(ROWS_KEY, raw, InboxRows)
        _require_owner(ROWS_KEY, rows.subject, self._subject())
        return rows

    async def _arm_compaction(self, due_at: datetime | None, now: datetime) -> datetime:
        """(Re-)register the compaction reminder when it is missing or overdue; return the next due time.

        Idempotent by construction: `register_reminder` OVERWRITES a reminder of the same name, so a
        repair can never produce a second one. Conditional on purpose — re-arming unconditionally would
        push the due time forward on every interaction, and an inbox that is read often would then
        never be compacted at all, which is the opposite of what the repair is for.
        """
        if due_at is not None and now < due_at:
            return due_at
        period = timedelta(seconds=get_notifications_settings().compaction_interval_seconds)
        await self.register_reminder(COMPACTION_REMINDER, b"", period, period)
        return now + period

    async def _disarm_compaction(self) -> None:
        """Unregister once the inbox is empty — there is nothing left to bound, and a reminder that
        outlives its rows wakes a pod up to look at nothing.

        Best-effort, because the rows are already persisted: raising here would turn a compaction that
        SUCCEEDED into a failed call and leave the trim it just did unreported. Logged rather than
        swallowed, because the benign case never reaches this handler — daprd answers a reminder DELETE
        with 204 whether or not one was registered, and the SDK raises only on a non-2xx or a transport
        fault. So anything caught here is a real failure, and the reminder it could not cancel goes on
        waking this actor at the compaction cadence to look at an empty inbox.
        """
        try:
            await self.unregister_reminder(COMPACTION_REMINDER)
        except Exception:
            logger.exception("inbox_compaction_disarm_failed")

    async def _repair_compaction(self, meta: InboxMeta) -> None:
        """Re-arm a reminder the Scheduler lost — the read path IS the repair path.

        The reminder lives in the Scheduler's etcd and the rows live in Postgres, with no transaction
        between them, so an inbox can end up holding rows with nothing scheduled to trim them. Any
        interaction with the subject repairs that. The meta write is what makes the repair durable;
        it happens only when the due time actually moved, so an ordinary read stays a read.
        """
        if meta.rows <= 0:
            return
        now = datetime.now(UTC)
        due_at = await self._arm_compaction(meta.compaction_due_at, now)
        if due_at == meta.compaction_due_at:
            return
        await self._save(meta.model_copy(update={"compaction_due_at": due_at, "updated_at": now}), rows=None)

    async def _save(self, meta: InboxMeta, rows: InboxRows | None) -> None:
        """Write the partitions that changed, in ONE transaction.

        `save_state` flushes every staged change together, so the count and the rows it was derived
        from move as a unit — a half-applied save is the one way this actor could hold two truths.
        The TTL rides both keys for the same reason: partitions that expire at different moments would
        read as a count with no rows, or rows with no count.
        """
        settings = get_notifications_settings()
        # `None` means "send no `ttlInSeconds` metadata", which is NOT the same as sending a TTL the
        # runtime ignores. daprd REFUSES the whole transaction when that metadata arrives with the
        # `ActorStateTTL` feature off (`pkg/actors/api/transactional.go`, still there at the vendored
        # v1.18.1), and this estate's only Dapr `Configuration` carries no `features:` block — so an
        # unconditional TTL would fail every write below rather than being free. See
        # `NotificationsSettings.actor_state_ttl_enabled`; the compaction reminder is the bound either way.
        ttl = settings.inbox_ttl_seconds if settings.actor_state_ttl_enabled else None
        if rows is not None:
            await self._state_manager.set_state_ttl(ROWS_KEY, rows.model_dump_json(), ttl)
        await self._state_manager.set_state_ttl(META_KEY, meta.model_dump_json(), ttl)
        await self._state_manager.save_state()

    async def _persist(self, pointers: list[InboxPointer], meta: InboxMeta | None) -> dict[str, Any]:
        """Store a new row set and the count derived from it, with the reminder armed FIRST.

        Arm before, disarm after. A reminder armed for rows that then fail to persist is benign — its
        tick finds an inbox it has nothing to do to and stands down — while the reverse order can
        persist rows whose only bound never got registered, and nothing downstream would ever notice.
        """
        now = datetime.now(UTC)
        subject = self._subject()
        due_at = await self._arm_compaction(meta.compaction_due_at if meta else None, now) if pointers else None

        unread = unread_count(pointers)
        await self._save(
            InboxMeta(subject=subject, unread=unread, rows=len(pointers), compaction_due_at=due_at, updated_at=now),
            rows=InboxRows(subject=subject, pointers=pointers, updated_at=now),
        )

        if not pointers:
            await self._disarm_compaction()
        return {"unread": unread, "rows": len(pointers)}

    @override
    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Land one pointer. Idempotent on `notification_id`, which is the dedupe.

        JetStream delivery is at-least-once and the reconciler will re-offer the same run over a second
        ingress path, so the natural key has to be the guard — there is no exactly-once to lean on and
        no window that survives a pod restart.
        """
        delivery = NotificationDelivery.model_validate(payload)
        meta = await self._read_meta()
        rows = await self._read_rows()
        pointers = list(rows.pointers) if rows is not None else []
        if any(pointer.notification_id == delivery.notification_id for pointer in pointers):
            # Repaired here too, because this is the ONE turn that touches an inbox without reaching
            # `_persist`: an at-least-once redelivery of a run the subject already has would otherwise
            # reactivate the actor, find nothing to do, and leave a reminder the Scheduler lost still
            # lost — the rows it was meant to bound outliving the retention window in an inbox whose
            # owner never opens the panel. Conditional, like every other arming, so a pending reminder
            # is not pushed forward by redelivery traffic.
            if meta is not None:
                await self._repair_compaction(meta)
            return {"delivered": False, "unread": unread_count(pointers), "rows": len(pointers)}
        pointers.append(InboxPointer.arriving(delivery))
        return {"delivered": True, **await self._persist(pointers, meta)}

    @override
    async def page(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One page of the feed. An inbox nobody has ever written to is EMPTY, not unreadable."""
        query = InboxQuery.model_validate(payload)
        meta = await self._read_meta()
        if meta is None:
            return InboxPage(pointers=[], has_more=False, unread=0).model_dump(mode="json")
        await self._repair_compaction(meta)
        rows = await self._read_rows()
        return paginate(rows.pointers if rows is not None else [], query).model_dump(mode="json")

    @override
    async def unread(self) -> dict[str, Any]:
        """The badge, answered from the small partition alone — the reason the partitions are split."""
        meta = await self._read_meta()
        if meta is None:
            return {"unread": 0, "rows": 0}
        await self._repair_compaction(meta)
        return {"unread": meta.unread, "rows": meta.rows}

    @override
    async def mark_seen(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Mark the rows the panel actually rendered as read."""
        wanted = set(InboxMark.model_validate(payload).notification_ids)
        meta = await self._read_meta()
        rows = await self._read_rows()
        pointers = list(rows.pointers) if rows is not None else []
        # An inbox with no rows — never written, or already emptied — is a no-op. Falling through would
        # write an empty record for a subject nobody has notified, minting state out of a read.
        if not pointers:
            return {"updated": 0, "unread": 0, "rows": 0}
        updated = [pointer.marked_seen() if pointer.notification_id in wanted and not pointer.seen else pointer for pointer in pointers]
        changed = sum(1 for before, after in zip(pointers, updated, strict=True) if before is not after)
        return {"updated": changed, **await self._persist(updated, meta)}

    @override
    async def dismiss(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Dismiss one notification. Keyed by `run_id@STATE`, so dismissing a run's "started" leaves
        its later "failed" free to arrive — the property the shared component's id scheme exists for."""
        target = InboxDismiss.model_validate(payload).notification_id
        meta = await self._read_meta()
        rows = await self._read_rows()
        pointers = list(rows.pointers) if rows is not None else []
        if not pointers:
            return {"dismissed": 0, "unread": 0, "rows": 0}
        updated = [pointer.marked_dismissed() if pointer.notification_id == target and not pointer.dismissed else pointer for pointer in pointers]
        changed = sum(1 for before, after in zip(pointers, updated, strict=True) if before is not after)
        return {"dismissed": changed, **await self._persist(updated, meta)}

    @override
    async def receive_reminder(self, name: str, state: bytes, due_time: timedelta, period: timedelta, ttl: timedelta | None = None) -> None:
        """The compaction tick — the only authoritative bound on an inbox.

        An unreadable record is caught rather than raised: the reminder is repeating, so letting it
        propagate would retry a permanent failure at the runtime's own pace forever, while dropping
        THIS tick still leaves the next one to try at the compaction cadence. Either way the fact is
        logged, because an inbox that is both unreadable and unbounded is the thing an operator needs
        to know about.
        """
        if name != COMPACTION_REMINDER:
            return
        settings = get_notifications_settings()
        try:
            meta = await self._read_meta()
            rows = await self._read_rows()
        except InboxUnreadable:
            logger.exception("inbox_compaction_skipped")
            return
        pointers = compact(
            rows.pointers if rows is not None else [],
            now=datetime.now(UTC),
            ttl=timedelta(seconds=settings.inbox_ttl_seconds),
            max_rows=settings.inbox_max_rows,
        )
        await self._persist(pointers, meta)

    async def _read_watches(self) -> InboxWatches | None:
        has, raw = await self._state_manager.try_get_state(WATCHES_KEY)
        if not has or not raw:
            return None
        watches = _parse(WATCHES_KEY, raw, InboxWatches)
        _require_owner(WATCHES_KEY, watches.subject, self._subject())
        return watches

    async def get_watches(self) -> dict[str, Any]:
        """The projects this subject watches. Absent state reads as an EMPTY list, never as an error.

        Absent is the ordinary case — most subjects watch nothing — and it is genuinely different from
        the unreadable case, which `_require_owner` still raises on.
        """
        watches = await self._read_watches()
        return {"projects": list(watches.projects) if watches else [], "total": len(watches.projects) if watches else 0}

    async def set_watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add or remove ONE project. Idempotent in both directions.

        No `ttlInSeconds` here, unlike the pointer rows: a watch is a STANDING preference, not a
        notification that ages out. Expiring it would silently stop delivering to someone who never
        asked to stop being told — the failure this plane exists to avoid, arriving as a silence.
        """
        project_id = str(payload["project_id"])
        watching = bool(payload["watching"])
        current = await self._read_watches()
        projects = list(current.projects) if current else []
        if watching and project_id not in projects:
            projects.append(project_id)
            changed = True
        elif not watching and project_id in projects:
            projects = [p for p in projects if p != project_id]
            changed = True
        else:
            changed = False
        if changed:
            record = InboxWatches(subject=self._subject(), projects=projects, updated_at=datetime.now(UTC))
            await self._state_manager.set_state(WATCHES_KEY, record.model_dump_json())
            await self._state_manager.save_state()
        return {"projects": projects, "total": len(projects), "changed": changed}
