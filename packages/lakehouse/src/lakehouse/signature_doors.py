"""The settings a verifying bus door reads: its mode, and the identities that sign the lakehouse's events.

A door that verifies a lineage event or a catalog control event before it acts on it (the catalog's and maintenance's
control-lane consumers) mixes :class:`SignatureDoorSettings` into its own settings. The signer roles are the control
lane's (:data:`~lakehouse.control_events.ControlSigner`), part of the contract the lakehouse publishes for its events.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from lakehouse.control_events import ControlSigner
from lakehouse.lineage.door import DoorMode


class SignatureDoorSettings:
    """WHAT A VERIFYING BUS DOOR BELIEVES ([[XC-078]]): its mode and the signer sets the chart renders for it.

    `event_signers` and `event_delegators` are the sets lineage verifies lineage events with (`lance.signingIdentities`,
    `lance.delegatorIdentities`): the identities that may sign, and those among them that may sign for a person.
    `control_signer_roles` maps each `ControlSigner` role to the identities that sign as it; a door verifies a control
    event against the identities of the role `control_events.control_signer_role` names for its action, with the same
    `event_delegators`. A role this code does not know is a boot error, so a chart and an image that disagree on the
    role names fail at startup instead of refusing every event of the role.

    A DOOR THAT VERIFIES NAMES ITS SIGNERS. OBSERVE or ENFORCE with no `event_signers` is a boot error, as lineage's
    chart guard is a render error: an empty set verifies nothing and refuses everything, so an enforcing door would
    acknowledge every honest event away and an observing one would report every event as one it would refuse.

    A plain mixin, like the auth mixins in `lakehouse.governed.settings`, so it takes the host class's own `model_config`.
    """

    #: How the door treats an event's signature: the chart's `signing.doors`.
    signature_doors: DoorMode = Field(default=DoorMode.OFF, alias="RASK_SIGNATURE_DOORS")
    event_signers: frozenset[str] = Field(default=frozenset(), alias="RASK_EVENT_SIGNERS")
    event_delegators: frozenset[str] = Field(default=frozenset(), alias="RASK_EVENT_DELEGATORS")
    control_signer_roles: dict[ControlSigner, frozenset[str]] = Field(default_factory=dict, alias="RASK_CONTROL_SIGNER_ROLES")

    @model_validator(mode="after")
    def _a_verifying_door_names_its_signers(self) -> Self:
        if self.signature_doors is not DoorMode.OFF and not self.event_signers:
            raise ValueError(f"RASK_SIGNATURE_DOORS={self.signature_doors} needs RASK_EVENT_SIGNERS: a door would verify against no signer")
        return self
