"""No manifest this chart's OWN templates render may carry a null-valued key — in ANY overlay CI deploys.

A bare `key:` whose only content sits behind a guard is YAML null, not an empty mapping or list, and
the difference is invisible until an API server reads it. Measured 2026-09-24, two of them were live
at once and each took out a whole stack on the server-side apply:

    Configuration.dapr.io "lance-config-catalog" is invalid:
    spec.secrets.scopes[0].deniedSecrets: Invalid value: "null": must be of type array: "null"

    Component.dapr.io "lance-statestore" is invalid:
    auth: Invalid value: "null": auth in body must be of type object: "null"

`deniedSecrets` is `all - mine`, so a verifier that owns every identity denies nothing. `auth` holds
one `secretStore` line gated on `lance.secretsViaDapr`. Both render, both lint clean, and every gate
that reads the PARSED value sees "absent" — which is what they mean, to everything but the CRD.

ONE OVERLAY IS NOT COVERAGE, and that is the other half of the lesson. The first cut of this gate
rendered the local-development overlay, found the two `deniedSecrets` and reported the class clean.
`e2e-ray` then failed on `lance-statestore` — its overlay sets `openbao.enabled=false`, which the
local one does not, and the null only exists there. So the overlays are READ OUT OF the stack
scripts CI runs rather than restated here: a script that changes its values changes what this gate
renders, in the same edit.

SCOPED TO `rask/templates/` via Helm's own `# Source:` comment. The vendored subcharts render nulls
of their own (nats' PDB annotations, greptimedb's serviceaccount command, perses' test-pod
imagePullPolicy); those belong to upstream and the next `helm dependency update` overwrites any edit.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from tests.unit.chart_render import DEFAULT_ARGS, OIDC_ARGS, render_text
from tests.unit.chart_yaml import FAST_LOADER


REPO = pathlib.Path(__file__).resolve().parents[2]

#: Documents from `rask/templates/...`; a subchart renders under `rask/charts/...`.
_OWN_SOURCE = re.compile(r"^# Source: rask/templates/(\S+)", re.MULTILINE)

#: The stack scripts whose `helm upgrade` overlay CI deploys. Read, not restated.
_STACK_SCRIPTS = ("e2e_stack.sh", "ray_e2e_stack.sh")

#: A shell variable inside a `--set` value. Substituted rather than skipped: dropping the flag would
#: quietly shrink the overlay, and the gate is about SHAPE, so any syntactically valid value serves.
_SHELL_VAR = re.compile(r"\$\{?\w+\}?")


def _overlay_from(script: pathlib.Path) -> tuple[str, ...]:
    """The `--set` / `--set-string` / `--set-json` flags this stack script hands helm."""
    args: list[str] = []
    # A double-quoted shell value may contain BACKSLASH-ESCAPED quotes — `--set-json
    # "k=[\\"$A\\",\\"$B\\"]"` is how the stack scripts pass a JSON list. A naive `"[^"]*"` stops at the
    # first of them and hands helm `[\\`, which fails the render and reads as a chart bug.
    pattern = r"""--(set|set-string|set-json)\s+("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S+)"""
    for flag, raw in re.findall(pattern, script.read_text(encoding="utf-8")):
        value = raw[1:-1] if raw[:1] in "\"'" else raw
        args += [f"--{flag}", _SHELL_VAR.sub("gate-placeholder", value.replace('\\"', '"').replace("\\'", "'"))]
    return tuple(args)


def _overlays() -> list[tuple[str, tuple[str, ...]]]:
    out = [("local-development", tuple(DEFAULT_ARGS))]
    for name in _STACK_SCRIPTS:
        script = REPO / "scripts" / name
        assert script.is_file(), f"{name} no longer exists — this gate names the overlays CI deploys"
        overlay = _overlay_from(script)
        assert overlay, f"{name} hands helm no --set flags — the overlay extraction has drifted"
        out.append((name, overlay))
    return out


def _null_paths(node: object, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}"
            found.append(here) if value is None else found.extend(_null_paths(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            here = f"{path}[{index}]"
            found.append(here) if value is None else found.extend(_null_paths(value, here))
    return found


def _own_documents(overlay: tuple[str, ...]) -> list[tuple[str, str, dict]]:
    """`(template, kind/name, parsed doc)` for every document this chart's own templates render."""
    out = []
    for chunk in render_text(*overlay, *OIDC_ARGS).split("\n---\n"):
        source = _OWN_SOURCE.search(chunk)
        if not source:
            continue
        doc = next(iter(yaml.load_all(chunk, Loader=FAST_LOADER)), None)
        if not isinstance(doc, dict) or not doc:
            continue
        name = (doc.get("metadata") or {}).get("name") or "<unnamed>"
        out.append((source.group(1), f"{doc.get('kind')}/{name}", doc))
    return out


@pytest.mark.parametrize("label,overlay", _overlays(), ids=lambda v: v if isinstance(v, str) else "")
def test_the_overlay_rendered_documents_to_check(label: str, overlay: tuple[str, ...]) -> None:
    """A control per overlay: an empty document list would make the gate below pass by vacuum."""
    own = _own_documents(overlay)
    assert len(own) > 30, f"{label} rendered only {len(own)} chart-owned documents — the gate lost its subject"


@pytest.mark.parametrize("label,overlay", _overlays(), ids=lambda v: v if isinstance(v, str) else "")
def test_no_chart_owned_document_carries_a_null_value(label: str, overlay: tuple[str, ...]) -> None:
    offenders = [f"{template} -> {obj}{path}" for template, obj, doc in _own_documents(overlay) for path in _null_paths(doc)]
    assert not offenders, (
        f"under the {label} overlay these chart-owned keys render as YAML null, which an API server "
        f"may refuse as a type error (Dapr's CRDs do). Emit the key only when it has content, or give "
        f"it an explicit empty value of the right type:\n  " + "\n  ".join(sorted(offenders))
    )
