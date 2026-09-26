"""The held-promotion payload, kept clear of the workflow engine on purpose.

A promotion spec is a WIRE AND STATE contract — a stage runner publishes it on the hold topic, the
review workflow carries it as its serialized input, and the promotions router parses it back out — so
it belongs beside the estate's other payload contracts rather than inside the engine adapter that
happens to consume it.

IT LIVES HERE BECAUSE OF WHAT IMPORTS IT. `api/promotions.py` is a router the producer mounts
unconditionally, and while this class sat in `workflow.py` — whose module body is
`import dapr.ext.workflow as wf` — importing the cascade head pulled the whole durabletask stack in
with it. The owner's condition 3 is that Dapr Workflow is something the lakehouse can be driven BY and
never something it depends ON, and an import is a dependency. Nothing about the payload itself needs
the engine: it is plain pydantic, and the engine adapter imports it rather than the other way round.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PromotionSpec(BaseModel):
    """One held promotion, carrying everything needed to resume it (B1: pointers, never payload)."""

    token: str
    project: str = ""
    #: The four names the held stage resolved (`resolve_stage_identity`): catalog ids, already
    #: tenant-qualified on an env lane and exactly as declared on a declared one. Every consumer —
    #: lineage nodes, FGA objects, the catalog publish — uses them AS GIVEN; re-qualifying one names
    #: an object the stage never wrote and no grant mentions.
    from_namespace: str
    from_dataset: str
    to_namespace: str
    to_dataset: str
    #: WHICH assertions failed. On the SPEC, set by the caller from the run's own result — never read
    #: from settings inside the body, where a value could change under a running instance.
    reasons: list[str] = Field(default_factory=list)
    #: Who may answer. Resolved at hold time, because by the time this runs there is no request left
    #: to derive an identity from. Empty means nobody can be asked — which BLOCKS, never promotes.
    approver: str = ""
    originator: str = ""
    approval_hours: int = 72
    #: The version the hold was taken on. The resume must publish THIS one — a later commit may have
    #: landed while the approver was deciding, and publishing that would ship a version nobody
    #: reviewed. At least 1: Lance numbers a dataset's first version 1 (measured on pylance 12.0.0),
    #: so a hold is only ever taken on something the catalog can be asked to publish.
    version: int = Field(ge=1)
    #: The HELD STAGE's own lineage identity, resolved at hold time in the stage runner. The outcome is
    #: emitted by the PRODUCER, whose settings describe no stage, so a hold that cannot name its stage
    #: is refused at the hold topic rather than recorded under the producer's defaults.
    operation: str = Field(min_length=1)
    author: str = Field(min_length=1)
