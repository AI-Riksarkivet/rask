"""What every step of the audit says of one object it did not act on: a refusal, or a failure.

The purge, the release, the tombstone sweep, the drift repair and the tuple rebuild each report the objects they left as
they were. A refusal is a finding (the step judged the object and kept it); a failure is a step that could not judge or
could not act. The two are told apart in every report, and each names the object the same way, so one reader serves all
five.
"""

from pydantic import BaseModel


class Refused(BaseModel):
    """One object a step considered and left as it is, and why."""

    kind: str
    id: str
    #: The uuid the object's record is filed under, where it has one: two drops of one name are two records ([[LH-525]]).
    uuid: str | None = None
    reason: str


class Failed(BaseModel):
    """One object a step could not judge or could not act on, and the failure. The next run asks again."""

    kind: str
    id: str
    uuid: str | None = None
    error: str


def named(fga_object: str) -> tuple[str, str]:
    """The kind and the id an OpenFGA object (``kind:id``) names."""
    kind, _, obj_id = fga_object.partition(":")
    return kind, obj_id
