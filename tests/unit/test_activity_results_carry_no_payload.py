"""B1, generalised: an activity result may carry identifiers and counts, never payloads.

`docs/architecture/batch-processing-invariants.md` B1 — "History and events carry identifiers and counts, never payloads."
Every activity result is persisted into workflow history and replayed on every recovery, so a result
that carries rows or bytes multiplies into the state store: once as the output, again per dependent,
again on every replay. The estate has the ceilings (`dapr_publish.MAX_PAYLOAD_BYTES`, ingest's
`CHUNK_DISPATCH_BUDGET_BYTES`) and two hand-named models pinned by size — and nothing that notices a
NEW activity added tomorrow whose return type carries a payload.

That is the gap this closes. The gate walks the ACTIVITIES tuple of each workflow module, so it
covers whatever is registered rather than whatever somebody remembered to name.

WHY `bytes` AND NOT "anything list-shaped". A pointer list is legitimate and load-bearing:
`enumerate_chunks` returns chunk DESCRIPTORS, bounded by its own dispatch budget, and refusing
`list[dict]` would refuse the design. Raw bytes in a result have no such reading — there is no
bounded amount of them that belongs in workflow history, which is why `staging.write_unit_manifest`
exists to put the (key, token) list in object storage and hand back an offset and a count.

So the rule is narrow enough to be true: no activity return annotation, and no field of any Pydantic
model it names, may be `bytes` or `bytearray`.
"""

from __future__ import annotations

import re
import typing

import pytest
from pydantic import BaseModel

from ingest import workflow as ingest_wf
from medallion import workflow as medallion_wf


MODULES = [("medallion", medallion_wf), ("ingest", ingest_wf)]

#: Types that are a PAYLOAD by construction. Not a heuristic — there is no bounded quantity of raw
#: bytes that belongs in replayed history.
FORBIDDEN = (bytes, bytearray, memoryview)


def _activities(module: object) -> tuple:
    activities = getattr(module, "ACTIVITIES", None)
    assert activities, f"{module} exposes no ACTIVITIES tuple — this gate would check nothing"
    return activities


#: Names in an annotation STRING that denote a payload. These modules use
#: `from __future__ import annotations`, so every annotation is already a string and several name
#: types that only exist under TYPE_CHECKING — `typing.get_type_hints` raises NameError on them. The
#: strings are what a reviewer reads anyway, so the gate reads the same thing.
_FORBIDDEN_RE = re.compile(r"\b(bytes|bytearray|memoryview)\b")


def _annotations(fn: object) -> dict[str, str]:
    return {k: str(v) for k, v in getattr(fn, "__annotations__", {}).items()}


def _models_in(annotation: object) -> list[type[BaseModel]]:
    """Every Pydantic model reachable from a type annotation, including inside list/dict/unions."""
    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    for arg in typing.get_args(annotation):
        found.extend(_models_in(arg))
    return found


def _models_named_in(text: str, module: object) -> list[type[BaseModel]]:
    """Pydantic models the annotation STRING names, resolved off the module that declared it."""
    out: list[type[BaseModel]] = []
    for word in set(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", text)):
        candidate = getattr(module, word, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            out.append(candidate)
    return out


def _forbidden_in(annotation: object) -> bool:
    if isinstance(annotation, str):
        return bool(_FORBIDDEN_RE.search(annotation))
    if annotation in FORBIDDEN:
        return True
    return any(_forbidden_in(arg) for arg in typing.get_args(annotation))


class TestNoActivityReturnsRawBytes:
    @pytest.mark.parametrize(("name", "module"), MODULES, ids=[n for n, _ in MODULES])
    def test_every_registered_activity(self, name: str, module: object) -> None:
        offenders = []
        for fn in _activities(module):
            if _forbidden_in(_annotations(fn).get("return", "")):
                offenders.append(fn.__name__)
        assert offenders == [], (
            f"{name}: {offenders} return raw bytes from an activity. Every activity result is written "
            f"into workflow history and replayed on every recovery — put the bytes in object storage "
            f"and return a pointer, as `write_unit_manifest` does."
        )


class TestNoResultMODELCarriesBytes:
    @pytest.mark.parametrize(("name", "module"), MODULES, ids=[n for n, _ in MODULES])
    def test_models_named_by_an_activity_signature(self, name: str, module: object) -> None:
        """The indirection the hand-named pins missed: a result model is a payload carrier whether
        the activity names `bytes` directly or names a model with a `bytes` field."""
        offenders: list[str] = []
        for fn in _activities(module):
            for annotation in _annotations(fn).values():
                for model in _models_named_in(annotation, module):
                    for field, info in model.model_fields.items():
                        if _forbidden_in(info.annotation):
                            offenders.append(f"{fn.__name__} -> {model.__name__}.{field}")
        assert offenders == [], f"{name}: {offenders} put raw bytes in a replayed activity payload"


class TestTheGateCannotGoVacuous:
    """Three ways a registry-driven gate silently stops checking: the tuple empties, the module stops
    resolving, or the annotations disappear so there is nothing to inspect."""

    @pytest.mark.parametrize(("name", "module"), MODULES, ids=[n for n, _ in MODULES])
    def test_the_registry_is_populated(self, name: str, module: object) -> None:
        assert len(_activities(module)) >= 5

    @pytest.mark.parametrize(("name", "module"), MODULES, ids=[n for n, _ in MODULES])
    def test_every_activity_declares_a_return_type(self, name: str, module: object) -> None:
        missing = [fn.__name__ for fn in _activities(module) if "return" not in _annotations(fn)]
        assert missing == [], f"{name}: {missing} declare no return type, so this gate cannot inspect them"

    def test_the_detector_actually_finds_bytes(self) -> None:
        """A detector nobody has watched fail is a detector nobody knows works."""

        class _Carrier(BaseModel):
            payload: bytes

        assert _forbidden_in(bytes)
        assert _forbidden_in(list[bytes])
        assert _forbidden_in(dict[str, bytes] | None)
        assert not _forbidden_in(dict[str, str])
        assert _models_in(list[_Carrier]) == [_Carrier]
        # ...and the STRING form, which is what these modules actually carry.
        assert _forbidden_in("list[bytes]")
        assert _forbidden_in("dict[str, bytearray] | None")
        assert not _forbidden_in("dict[str, Any]")
        assert not _forbidden_in("StageJobOutcome"), "a model NAME is checked field-by-field, not by regex"
