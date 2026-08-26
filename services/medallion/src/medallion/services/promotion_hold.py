"""The mover's side of the quality gate's third answer: publish the HOLD instead of ruling on it.

`_QUALITY_BLOCKED` is a permanent DROP, and for a corrupt blob pointer or a null key that is exactly
right — no approval makes broken data correct. It is the wrong answer for a promotion that is merely
UNUSUAL: a batch that legitimately shipped zero rows, a declared column a consumer already agreed to
drop. Those are decisions, and a mover has nobody to ask.

Splitting the two is NOT done here. This module publishes what the gate saw; `promotion_review` — which
runs in the producer, beside the door a person can reach — decides whether the hold is corrupt (BLOCK),
unusual (ask) or clean. Deciding it in two places is how the two answers drift apart.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from medallion.core.config import MedallionSettings
from medallion.workflow import PromotionSpec
from service_kit import dapr_publish


log = logging.getLogger(__name__)


def review_enabled(settings: MedallionSettings) -> bool:
    """Whether a hold becomes a question. Off by default: an estate with nobody to ask must keep the
    permanent BLOCK rather than parking promotions on an event no one will ever raise.

    Deliberately NOT gated on an approver being configured — the workflow answers "no reachable
    approver" in the outcome and in lineage, where an operator can see it. Swallowing it here would
    reproduce the unexplained DROP the review exists to replace.
    """
    return settings.quality_review_enabled


def hold_spec(
    settings: MedallionSettings,
    *,
    token: str,
    project: str,
    from_namespace: str,
    from_dataset: str,
    to_namespace: str,
    to_dataset: str,
    reasons: list[str],
    originator: str,
    version: int = 0,
) -> PromotionSpec:
    """Everything the review needs to resume the cascade, resolved at DISPATCH.

    The deadline, the approver and the downstream topic all ride the spec rather than being read
    inside the workflow: a body that reads settings replays against whatever the value is now instead
    of what it was when the promotion was held. `pub_topic` matters most — the producer hosting the
    review has no idea what this mover's next hop is, so without it an approval records a decision and
    promotes nothing.
    """
    return PromotionSpec(
        token=token,
        project=project,
        from_namespace=from_namespace,
        from_dataset=from_dataset,
        to_namespace=to_namespace,
        to_dataset=to_dataset,
        pub_topic=settings.pub_topic,
        reasons=reasons,
        approver=settings.quality_review_approver,
        originator=originator,
        approval_hours=settings.quality_review_hours,
        # The version the hold was taken on. An approval resumes by publishing THIS one — a later
        # commit may land while the approver decides, and publishing that would ship a version nobody
        # reviewed.
        version=version,
        # Resolved HERE because here is the only place that knows: this runs in the mover, so
        # `settings` is the held stage's. The producer that emits the outcome reads its own settings
        # and sets neither var, so before these rode the spec every approved promotion was recorded
        # as `embed_features`/`data_eng` — right by accident for a silver hold, wrong for every
        # other lane. Same reason `pub_topic` is resolved at dispatch rather than in the workflow.
        operation=settings.operation,
        author=settings.author,
    )


async def publish_hold(dapr: Any, settings: MedallionSettings, spec: PromotionSpec) -> bool:
    """Publish the hold, reporting whether it landed.

    Returns rather than raises: the caller has already written its output and emitted the held-run
    lineage, so a broker blip must degrade to the old permanent BLOCK — which is safe — not unwind a
    completed write or retry the whole transform.
    """
    try:
        await dapr_publish.publish_event(
            dapr,
            timeout_seconds=settings.publish_timeout_seconds,
            pubsub_name=settings.pubsub,
            topic_name=settings.promotion_topic,
            data=json.dumps(spec.model_dump()),
            data_content_type="application/json",
        )
    except Exception:
        log.warning(
            "medallion_promotion_hold_not_published",
            extra={"token": spec.token, "dataset": spec.to_dataset, "topic": settings.promotion_topic},
            exc_info=True,
        )
        return False
    log.info(
        "medallion_promotion_held_for_review",
        extra={"token": spec.token, "dataset": spec.to_dataset, "reasons": spec.reasons, "approver": spec.approver},
    )
    return True
