"""Shape guards for the pointer triggers this service consumes off the bus.

The bus is a WIDER TRUST SURFACE than any token-guarded HTTP head: a trigger arrives carrying whatever
its publisher — or anything else that can reach the topic — put on it, and its fields become Lance read
URIs, Ray submission ids and lineage graph values. So every consumed field is validated HERE and a
payload that fails is **DROPped** — never repaired, and never raised: redelivery cannot fix
deterministic garbage and a raising handler poisons the subscription (DATA-CONTRACT §7.3).

Two rules, and they are different kinds of thing:

* :func:`safe_token` is a SHAPE rule, and it is deliberately the ``Idempotency-Key`` contract the
  estate's own heads publish (``^[A-Za-z0-9._-]+$``, max 64 — ``/produce``, ``/ingest-media``,
  ``/train``), hardened by refusing ``..``. A consumer stricter than the head that feeds it does not
  add safety, it silently DROPs cascades the head already 202'd. (``services/train.py``'s private
  ``_safe_name`` IS that consumer — it rejects the dots its own head accepts. Reconciling the two is
  a follow-up in a file this change does not own; ``test_medallion_trigger_guards.py`` pins the
  relation between the grammars meanwhile, so neither can drift unnoticed.)
* :func:`uri_within` is a SECURITY boundary. A trigger may NAME the upstream it wants read (I2 —
  resolve the location through the catalog, never compose a path), but the mover opens that name with
  its OWN object-store credentials, so it is honoured only inside the storage root the stage already
  resolved. Unbounded, the field is a read primitive for everything that credential can reach.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


#: A correlation token, as every head that mints one already constrains it: ``Idempotency-Key``'s
#: ``^[A-Za-z0-9._-]+$`` with its ``max_length=64``. It admits the two machine-minted shapes the
#: cascade actually carries (a ``uuid4().hex`` and a dashed UUID) and excludes what matters — path
#: separators, ``$``, whitespace, control characters, and anything unbounded. The token SEEDS the
#: deterministic lineage run id (``schemas/events.build_run_event``), rides the ``lance`` run facet
#: into the graph, and reaches ``ray_kit.submit.submission_id``. That last one is not an injection
#: sink — it folds every character outside ``[A-Za-z0-9_-]`` to ``-`` and truncates at 200 — but
#: folding is exactly why the shape matters: two different tokens can land on ONE submission id, and
#: ``submit_or_reattach`` reads that collision as a successful re-attach, so the second stage's work
#: silently never runs (the failure mode ``submission_id``'s own docstring exists to describe).
SAFE_TOKEN_PATTERN = r"[A-Za-z0-9._-]{1,64}"  # noqa: S105 — a shape regex; the cascade token is a correlation id, not a credential
_SAFE_TOKEN = re.compile(SAFE_TOKEN_PATTERN)


def safe_token(value: object) -> bool:
    """True iff ``value`` is a well-shaped correlation token.

    ``fullmatch`` (not ``search``) so there is no trailing-newline leniency, and ``..`` is refused
    outright: it is the one dotted value that is a traversal rather than a name, and the estate's
    header pattern admits it.
    """
    return isinstance(value, str) and _SAFE_TOKEN.fullmatch(value) is not None and ".." not in value


def uri_within(base: str, candidate: str) -> bool:
    """True iff ``candidate`` names a location inside ``base``'s storage domain.

    Prefix containment against ``base`` + ``/`` — never a bare prefix, which is what stops
    ``s3://acme-wh-evil/x`` from passing for the root ``s3://acme-wh`` — plus two refusals prefix
    containment alone does not give: a ``..`` segment (which satisfies the prefix and then climbs back
    out), and any whitespace or C0 control character — not because either is an injection downstream
    (the URI travels as a JSON value into a Ray job's ``runtime_env`` and into emitted lineage, both
    of which escape it), but because no location the catalog can vend contains one, so its presence
    is evidence the value was not vended and the fail-closed answer is to refuse it.

    LIMIT, stated rather than implied: containment is LEXICAL. A percent-encoded ``%2e%2e`` is not
    decoded here and passes. That is not a hole today — the boundary this enforces is the storage
    DOMAIN (bucket/root), an S3 keyspace is flat so ``..`` cannot climb out of one, and a bare
    filesystem root is not percent-decoded by object_store — but a future caller that normalizes the
    URI before opening it would need this to normalize too.

    An empty ``base`` is never containment: a stage with no resolved root has no storage domain to
    confine a bus-supplied URI to, and "everything the credential can reach" is the wrong empty case.
    """
    if not base or not candidate:
        return False
    if any(ch.isspace() or ord(ch) < 0x20 for ch in candidate):
        return False
    if ".." in candidate.split("/"):
        return False
    stem = base.rstrip("/")
    return candidate == stem or candidate.startswith(f"{stem}/")


class StageTrigger(BaseModel):
    """One medallion stage trigger (DATA-CONTRACT §7.2) as a mover is willing to read it.

    ``extra="ignore"`` is the additive-only evolution rule (§7.4): a publisher may add optional fields
    — ``from_version``/``to_version`` already ride this payload — and an older consumer must tolerate
    them. Every field is optional because each is a CLAIM the handler may act on, not a requirement:
    an absent ``dataset`` makes no lane claim, an absent ``project`` means single-tenant, an absent
    ``from_uri`` means "compose the path". Present-and-wrong is what gets DROPped, not merely absent.

    ``project`` carries no pattern here on purpose — it is checked at its use site with
    ``warehouse_registry.is_safe_project``, the one definition two planes already share, and a second
    spelling of that rule is exactly the duplication this module exists to avoid. ``from_uri`` is not
    confined here either: containment depends on the root the stage resolves per-trigger (a registry
    lookup for a tenant, env otherwise), which the model cannot see — see :func:`uri_within`.
    """

    model_config = ConfigDict(extra="ignore")

    token: str | None = None
    dataset: str | None = None
    namespace: str | None = None
    project: str | None = None
    #: The HUMAN the cascade is running for, threaded from the head. Unvalidated here for the same
    #: reason `project` is: it is a claim, checked where it is used. It never authorizes anything —
    #: the notifications plane re-derives every recipient's visibility at delivery — so a forged one
    #: can at worst put a row in an inbox whose owner can already see the run's outputs.
    originator: str | None = None
    from_uri: str | None = None

    #: S1: the Ray stage job for this trigger reached SUCCEEDED, so the destination is written and the
    #: mover may measure it. Set ONLY by `medallion.workflow.publish_stage_ready`, after a terminal
    #: status read — never by an upstream producer, which has no way to know.
    #:
    #: This is a CLAIM like every other field on this model, and it is deliberately not treated as
    #: privileged: the re-published trigger re-enters through this same guard, and the worst a forged
    #: `ray_job_done` can do is make the mover measure a destination early — precisely the pre-S1
    #: behaviour, not an escalation. What it must NOT do is skip the submit silently, which is why the
    #: handler logs the branch it took.
    ray_job_done: bool = False
    #: Seconds the RAY WATCH took, measured by `stage_run` from its own deterministic workflow clock
    #: and handed back on the wake-up trigger. Pass 2 records THIS as the stage duration rather than
    #: its own elapsed time, which would measure only the measure/emit tail and report a multi-hour
    #: Ray job as a few seconds. One recording site, one number — recording in both the watcher and
    #: the handler would double-count every Ray stage in the histogram.
    #:
    #: BOUNDED because the trigger is untrusted input: a negative or absurd value is discarded rather
    #: than recorded, so a malformed payload cannot poison the series. 30 days is far beyond any real
    #: stage and comfortably above the watch ceiling.
    ray_duration_seconds: float | None = Field(default=None, ge=0.0, le=2_592_000.0)
    ray_submission_id: str | None = None

    #: R26's ONE INSTANT, carried from the dispatch pass to the completed pass. `transform.py` stamps
    #: `datetime.now(UTC)` at the top of the handler, so the two-pass Ray lane stamped TWICE: pass 1's
    #: instant went into the `lineage` JSONB the Ray job writes INTO the dataset, pass 2's onto the
    #: COMPLETE published to the graph. The dataset and the graph then disagree on the only field a
    #: consumer can join runs by time on — permanently, on every distributed run.
    #:
    #: Validated like every other field: a malformed timestamp would build a spec-invalid RunEvent and
    #: fail the emit AFTER the data landed, which is the worst moment to discover it.
    event_time: str | None = None

    @field_validator("event_time")
    @classmethod
    def _event_time_is_an_instant(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from datetime import datetime

        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("event_time must be an ISO-8601 instant") from exc
        return value

    @field_validator("token")
    @classmethod
    def _token_is_well_shaped(cls, value: str | None) -> str | None:
        if value is not None and not safe_token(value):
            raise ValueError("token must match the Idempotency-Key shape and carry no traversal")
        return value


def parse_stage_trigger(event: Any) -> StageTrigger | None:  # noqa: ANN401 — the untrusted CloudEvent envelope
    """The Dapr CloudEvent envelope → a validated :class:`StageTrigger`, or ``None`` when it is not one.

    Returns rather than raises so the caller can DROP: the handler contract is a status dict, and an
    exception escaping into the subscription route is the failure mode §7.3 exists to prevent.
    """
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return None
    try:
        return StageTrigger.model_validate(data)
    except ValueError:  # pydantic's ValidationError is a ValueError — a malformed field, not an outage
        return None
