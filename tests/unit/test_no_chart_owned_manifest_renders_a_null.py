"""No manifest this chart's OWN templates render may carry a null-valued key.

A bare `key:` over a range that yielded nothing is YAML null, not an empty list, and the difference
is invisible until an API server reads it. Measured 2026-09-24, `e2e-stack` died on the server-side
apply for BOTH lakehouse verifiers at once:

    Configuration.dapr.io "lance-config-catalog" is invalid:
    spec.secrets.scopes[0].deniedSecrets: Invalid value: "null":
    spec.secrets.scopes[0].deniedSecrets in body must be of type array: "null"

`deniedSecrets` is `all - mine`, so an app that owns every identity in the estate denies nothing and
emitted the key with no items. `helm template` renders it, `helm lint` accepts it, and the chart's
other gates read the parsed value as "absent" — which is exactly what it means, to everything except
the CRD's schema. The whole class is one grep away once the chart is rendered, so it is gated as a
class rather than at the one field that happened to fail.

SCOPED TO `rask/templates/`, from the `# Source:` comment Helm emits. The vendored subcharts render
three nulls of their own (nats' pod-disruption-budget annotations, greptimedb's serviceaccount
command, perses' test pod imagePullPolicy); those are upstream's to fix and editing them would be
overwritten by the next `helm dependency update`.
"""

from __future__ import annotations

import re

import yaml

from tests.unit.chart_render import DEFAULT_ARGS, OIDC_ARGS, render_text
from tests.unit.chart_yaml import FAST_LOADER


#: Documents from `rask/templates/...`; a subchart renders under `rask/charts/...`.
_OWN_SOURCE = re.compile(r"^# Source: rask/templates/(\S+)", re.MULTILINE)


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


def _own_documents() -> list[tuple[str, str, dict]]:
    """`(template, object name, parsed doc)` for every document this chart's own templates render."""
    out = []
    for chunk in render_text(*DEFAULT_ARGS, *OIDC_ARGS).split("\n---\n"):
        source = _OWN_SOURCE.search(chunk)
        if not source:
            continue
        doc = next(iter(yaml.load_all(chunk, Loader=FAST_LOADER)), None)
        if not isinstance(doc, dict) or not doc:
            continue
        name = (doc.get("metadata") or {}).get("name") or "<unnamed>"
        out.append((source.group(1), f"{doc.get('kind')}/{name}", doc))
    return out


def test_the_render_produced_documents_to_check() -> None:
    """A control: an empty document list would make the gate below pass by vacuum."""
    own = _own_documents()
    assert len(own) > 50, f"only {len(own)} chart-owned documents rendered — the gate lost its subject"


def test_no_chart_owned_document_carries_a_null_value() -> None:
    offenders = []
    for template, obj, doc in _own_documents():
        for path in _null_paths(doc):
            offenders.append(f"{template} -> {obj}{path}")
    assert not offenders, (
        "these chart-owned keys render as YAML null, which an API server may refuse as a type error "
        "(Dapr's Configuration CRD does, for deniedSecrets). Emit the key only when it has content, "
        "or give it an explicit empty value of the right type:\n  " + "\n  ".join(sorted(offenders))
    )
