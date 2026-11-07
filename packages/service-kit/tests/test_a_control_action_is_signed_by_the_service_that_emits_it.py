"""A control action names the one role whose signature a door accepts for it (ADR 0171).

`control_signer_role` is the published contract a notifier verifies the lakehouse's signed control events by: a
valid signature from another service is no authority over the action. The catalog arm is driven through the catalog's
own door (`services/catalog/tests/test_the_control_lane_acts_only_on_signed_events.py`); this pins the arm no door
test reaches, the expiry purge maintenance's sweep emits.
"""

from __future__ import annotations

from service_kit.control_events import control_signer_role


def test_an_expiry_purge_is_signed_by_maintenance() -> None:
    assert control_signer_role("table_purged") == "maintenance"
