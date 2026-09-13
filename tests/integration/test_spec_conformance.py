"""Conformance: every lance-namespace spec operation has a served catalog route (todo P1 #9).

Locks the catalog as a FAITHFUL REST surface over the spec — a spec op never wired, or a route renamed
away from the spec, turns this red. Routes are read from ``app.openapi()`` (NOT ``app.routes``): starlette
1.3's lazy ``include_router`` keeps included routes off ``app.routes``, so ``openapi()`` is the authoritative
served set. The app may serve MORE than the spec (the ``/credentials`` vending extension + health probes) —
we assert only that the spec is a SUBSET of what's served, on (method, structural-path).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient


_SPEC = Path(__file__).resolve().parents[2] / "lance_docs" / "ns_catalog" / "spec.yaml"
_METHODS = frozenset({"get", "put", "post", "delete", "patch"})


def _ops(paths: dict[str, Any]) -> set[tuple[str, str]]:
    """(METHOD, structural-path) per operation — path-param NAMES collapsed so ``{id}`` matches ``{x}``."""
    out: set[tuple[str, str]] = set()
    for path, item in paths.items():
        structural = re.sub(r"\{[^}]+\}", "{}", path)
        out.update((m.upper(), structural) for m in item if m.lower() in _METHODS)
    return out


def _spec() -> dict[str, Any]:
    return yaml.safe_load(_SPEC.read_text())


def test_every_spec_operation_is_served(client: TestClient) -> None:
    spec_ops = _ops(_spec().get("paths", {}))
    served = _ops(client.app.openapi().get("paths", {}))
    missing = spec_ops - served
    assert not missing, f"spec operations with NO served route: {sorted(missing)}"


def test_spec_has_54_operations() -> None:
    # Guards against a stale/shrunken vendored spec silently weakening the conformance check above.
    assert len(_op_ids(_spec())) == 54


#: Where the spec is published. Named once so the freshness check below and any future fetch agree.
_UPSTREAM = "https://raw.githubusercontent.com/lance-format/lance-namespace/main/docs/src/spec.yaml"


def _op_ids(doc: dict[str, Any]) -> set[str]:
    return {op["operationId"] for item in doc.get("paths", {}).values() for op in item.values() if isinstance(op, dict) and op.get("operationId")}


def test_the_vendored_spec_still_matches_UPSTREAM() -> None:
    """The half the count above structurally cannot see: whether the SPEC GREW.

    `== 54` catches our vendored copy shrinking and nothing else. If upstream adds a 55th operation, our
    copy stays at 54, this suite stays green, and the estate is silently non-conformant against a spec
    nobody re-read — the conformance gate reporting on a document rather than on the contract.

    NETWORK-GATED and SKIPS without one, the same shape `_helm_template` uses for a missing binary: a
    gate that fails on an offline laptop teaches people to ignore it, and this one is about drift over
    weeks rather than about the commit in front of you.

    Compared by operation ID rather than by count, so "they added one and removed one" cannot pass.
    Measured 2026-09-09: upstream 1.0.0 / 54 ops, vendored 1.0.0 / 54 ops, zero difference either way.
    """
    import urllib.error
    import urllib.request

    import pytest

    try:
        with urllib.request.urlopen(_UPSTREAM, timeout=20) as response:  # noqa: S310 — a pinned https URL, not caller input
            upstream = yaml.safe_load(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"upstream spec unreachable ({exc}) — freshness is a drift check, not a commit gate")

    ours, theirs = _op_ids(_spec()), _op_ids(upstream)
    assert theirs, "upstream spec parsed to zero operations — the URL moved; re-point _UPSTREAM"
    assert not theirs - ours, (
        f"upstream defines operations the vendored spec does not: {sorted(theirs - ours)}. The catalog is "
        "non-conformant against the CURRENT spec, and the count guard above cannot see it — re-vendor "
        f"{_UPSTREAM} and wire the new routes"
    )
    assert not ours - theirs, (
        f"the vendored spec carries operations upstream has RETIRED: {sorted(ours - theirs)} — the "
        "conformance check is asserting against a contract that no longer exists"
    )


def _param_shapes(doc: dict[str, Any]) -> dict[str, str]:
    """``{operationId.paramName: canonical schema}`` for every query/path parameter an operation takes.

    READS PATH-LEVEL PARAMETERS TOO, and that is the whole point. OpenAPI lets a parameter sit on the
    PATH ITEM, applying to every operation under it, and the spec uses that for the shared `id` /
    `delimiter` / `branch` trio — and for `on`, the one that actually changed. An extractor that reads
    only `operation.parameters` finds nothing and reports "no structural change", which is what this
    function was written after doing.

    `$ref` parameters are resolved one hop into `components/parameters`, because a shape change there
    is the same class of change as an inline one and reporting the ref string would hide it.
    """
    components = doc.get("components", {}).get("parameters", {})

    def resolve(param: dict[str, Any]) -> dict[str, Any]:
        ref = param.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/components/parameters/"):
            return param
        return components.get(ref.rsplit("/", 1)[-1], {})

    shapes: dict[str, str] = {}
    for item in doc.get("paths", {}).values():
        shared = [resolve(p) for p in item.get("parameters", []) or []]
        for method, op in item.items():
            if method.lower() not in _METHODS or not isinstance(op, dict) or not op.get("operationId"):
                continue
            own = [resolve(p) for p in op.get("parameters", []) or []]
            for param in shared + own:
                name = param.get("name")
                if not name:
                    continue
                shapes[f"{op['operationId']}.{name}"] = json.dumps(param.get("schema"), sort_keys=True, default=repr)
    return shapes


def test_the_vendored_spec_matches_upstream_on_PARAMETER_SHAPES_too() -> None:
    """The half comparing operation IDs structurally cannot see: a CHANGED operation.

    Measured 2026-09-14, and it is why this exists rather than being a precaution. Upstream commit
    eb1de88e ("feat(spec)!: allow multiple columns for the merge insert on key", 2026-09-01) turned
    merge_insert's `on` from a single `string` into a repeatable array — a BREAKING change by its own
    marker. The op-id check above answered "zero difference either way" across it, because the operation
    is still called `MergeInsertIntoTable` and there are still 54 of them. The vendored copy sat 79 lines
    behind upstream carrying the superseded shape, while `.claude` instructions say to read `lance_docs/`
    and cite it — so the cited source showed a contract upstream had already replaced.

    Compares SHAPES, not prose: a reworded description does not red this, so re-vendoring stays a
    response to a real contract move rather than a chore. Network-gated and skipping, for the same
    reason the op-id check is — drift is measured in weeks, not in the commit in front of you.
    """
    import urllib.error
    import urllib.request

    import pytest

    try:
        with urllib.request.urlopen(_UPSTREAM, timeout=20) as response:  # noqa: S310 — a pinned https URL, not caller input
            upstream = yaml.safe_load(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"upstream spec unreachable ({exc}) — freshness is a drift check, not a commit gate")

    ours, theirs = _param_shapes(_spec()), _param_shapes(upstream)
    assert theirs, "upstream spec yielded no parameters — the URL moved or the shape changed; re-point _UPSTREAM"

    changed = {key: (ours[key], theirs[key]) for key in ours.keys() & theirs.keys() if ours[key] != theirs[key]}
    assert not changed, (
        "the vendored spec disagrees with upstream on parameter SHAPES, so the catalog is being checked "
        f"against a contract that has moved: {json.dumps(changed, indent=2, sort_keys=True)[:1200]} — re-vendor {_UPSTREAM}"
    )
    assert not theirs.keys() - ours.keys(), f"upstream operations take parameters the vendored spec does not declare: {sorted(theirs.keys() - ours.keys())}"
    assert not ours.keys() - theirs.keys(), f"the vendored spec declares parameters upstream has retired: {sorted(ours.keys() - theirs.keys())}"


_PROVENANCE = _SPEC.resolve().parents[1] / "PROVENANCE.md"


def test_the_provenance_pin_names_the_commit_the_vendored_BYTES_came_from() -> None:
    """A pin nothing checks is a comment, and a wrong one is worse than none.

    `lance_docs/PROVENANCE.md` exists so a reader citing the spec can tell which version of the contract
    they are reading. That only holds if the commit it names is the commit the bytes came from — so this
    fetches the spec AT THAT SHA and compares it to the vendored file. Re-vendoring without moving the
    pin, or moving the pin without re-vendoring, both red here.

    Pinned by SHA rather than by `main`, which is what makes it reproducible: the upstream-drift checks
    above ask "has the contract moved since we vendored", a question that changes answer over time,
    while this one asks "is the pin honest", which must not.
    """
    import urllib.error
    import urllib.request

    import pytest

    text = _PROVENANCE.read_text()
    shas = re.findall(r"\b([0-9a-f]{40})\b", text)
    assert shas, f"{_PROVENANCE.name} names no 40-character commit sha — the pin is the whole point of the file"
    sha = shas[0]

    url = f"https://raw.githubusercontent.com/lance-format/lance-namespace/{sha}/docs/src/spec.yaml"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310 — a pinned https URL, not caller input
            pinned = response.read().decode()
    except urllib.error.HTTPError as exc:
        # A SERVED ANSWER, so the network is fine and the PIN is the problem. Skipping here would make a
        # sha that names no commit indistinguishable from an offline laptop, which is how this check
        # would come to pass for a pin pointing at nothing — measured: a 40-zero sha skipped instead of
        # failing until these two were split.
        raise AssertionError(f"{_PROVENANCE.name} pins {sha}, which upstream answers {exc.code} for — the pin names no commit that exists") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"upstream unreachable ({exc}) — provenance is a drift check, not a commit gate")

    assert pinned == _SPEC.read_text(), (
        f"{_PROVENANCE.name} pins {sha}, but the vendored spec.yaml is NOT those bytes. Either the spec was "
        "re-vendored without moving the pin, or the pin was moved without re-vendoring — a pin naming a "
        "different commit than the bytes is worse than no pin at all."
    )
