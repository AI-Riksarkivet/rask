"""CONTRACT (LH-041): a publication that loses the tag-create race converges rather than failing.

`_set_tag` read the tag and then branched create-vs-update. Two concurrent FIRST publications of one
dataset both read absent, both call create, and the loser got a 409 for a publish that should simply
have moved the tag to the version it asked for.

The compare-and-set already exists and is the format's: measured on the installed pylance, `tags.create`
on an existing tag raises "Ref conflict error", while `tags.update` has no conditional form at all. So
the create's refusal is the ONLY arbitration available, and using it is the fix — the same shape
`records.create_json` uses for the registry, where a 409 means "someone else won" rather than "this
failed".
"""

from __future__ import annotations

from typing import Any, cast


def test_a_concurrent_FIRST_publication_converges_instead_of_409ing(monkeypatch) -> None:
    """CONTRACT (LH-041): `_set_tag` treats "the tag already exists" as convergence, not as failure.

    It read the tag and then branched create-vs-update, which is a TOCTOU: two concurrent FIRST
    publications of one dataset both read `None`, both call `create`, and the loser gets
    `TableTagAlreadyExistsError` — a 409 on a publish that should simply have moved the tag.

    THE ARBITRATION IS THE STORE'S, and it already exists. Measured on the installed pylance:
    `tags.create` on an existing tag raises "Ref conflict error: tag published already exists", while
    `tags.update` has NO conditional form at all — so the conflict the create raises is the only
    compare-and-set the format offers, and the fix is to USE it rather than to widen the read.

    This is the same shape `records.create_json` uses for the registry: attempt the store-arbitrated
    create, and read its refusal as "someone else won the race" rather than as an error.
    """
    from lance_namespace import TableTagAlreadyExistsError

    from catalog.services import publication

    calls: list[str] = []

    def _tag_version(*_a: object, **_kw: object) -> int | None:
        return None  # both racers see an absent tag

    def _create(*_a: object, **_kw: object) -> None:
        calls.append("create")
        raise TableTagAlreadyExistsError("tag 'published' already exists")  # the other racer won

    def _update(*_a: object, **_kw: object) -> None:
        calls.append("update")

    monkeypatch.setattr(publication, "_tag_version", _tag_version)
    monkeypatch.setattr(publication.dataplane, "create_tag", _create)
    monkeypatch.setattr(publication.dataplane, "update_tag", _update)

    publication._set_tag(cast("Any", None), {}, ["acme", "silver", "features"], "published", 7)

    assert calls == ["create", "update"], f"the lost race was not converged: {calls}"
