"""A `.Values` path no values file defines renders a nil ARTIFACT, not an empty string.

THE DEFECT THIS GENERALISES. `lance.dedicatedServiceToken` hashed
`printf "%s-%s" <identity> .Values.dapr.appApiToken`, and `dapr.appApiToken` was defined by no values
file in the repository — the chart's value is `dapr.appToken`. Go does not render an undefined value as
the empty string; it renders the literal `%!s(<nil>)`. So every privileged service credential in the
estate was `sha256("<identity>-%!s(<nil>)")[:40]`, a pure function of a PUBLIC identity name, and
rotating `dapr.appToken` changed none of them. One letter of drift between two spellings, and nothing
anywhere reported it: the chart rendered, the Secret was populated, the doors compared and matched.

WHY A GATE RATHER THAN THE FIX ALONE. That defect is invisible by construction — a misspelled path
produces a well-formed manifest holding a plausible-looking value, so neither `helm template`,
`helm lint`, a schema, nor a deploy can see it. The only reader who can is one that resolves each path
against the values tree, which is what this does. A bug found means the surrounding cases are worth
proving, not just the one that fired.

WHAT COUNTS AS SAFE, and the distinction is the whole gate. An unresolved path is fine when the
template GUARDS it — `default` supplies a value, `with`/`if` skip the block, `required` fails the render
loudly (fail-closed is a correct answer), `dig`/`hasKey` ask rather than assume. What is not fine is an
unresolved path interpolated BARE, because that is the only shape that silently reaches a manifest.

THE GATE IS PROVEN NON-VACUOUS rather than asserted to be. `test_the_scan_catches_the_defect_it_was
_written_for` runs the same scanner over the exact pre-fix line and requires it to flag it. A scanner
that silently matched nothing would otherwise pass this file forever, which is the failure mode the
finding itself was an instance of.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from chart_yaml import FAST_LOADER


REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = REPO / "chart/templates"
_VALUES = REPO / "chart/values.yaml"

#: A dotted `.Values.a.b.c` reference. Matches `$.Values...` and `$root.Values...` equally — the
#: receiver is irrelevant, only the path that follows `.Values` is.
_REFERENCE = re.compile(r"\.Values((?:\.[A-Za-z_][A-Za-z0-9_]*)+)")

#: Template functions that make an unresolved path SAFE: it is defaulted, skipped, asked about, or
#: turned into a loud render failure. Word-bounded, so `and` does not match inside `sha256sum`.
_GUARD = re.compile(r"\b(default|with|if|else|required|hasKey|ternary|empty|and|or|dig|coalesce)\b")

#: The pre-fix line, verbatim. The scan must flag this or it is measuring nothing.
_THE_DEFECT = '{{- printf "%s-%s" $identity $root.Values.dapr.appApiToken | sha256sum | trunc 40 -}}'


def _values_tree() -> dict[str, Any]:
    """The chart's default values, as the render will see them.

    `load_all` rather than `load` for the reason the sibling chart gates use it: it is the repo's one
    spelling for the shared fast loader, and a values file that grew a second document would be a
    render whose second half this gate could not see.
    """
    documents = [doc for doc in yaml.load_all(_VALUES.read_text(encoding="utf-8"), Loader=FAST_LOADER) if doc is not None]
    assert len(documents) == 1 and isinstance(documents[0], dict), f"chart/values.yaml is not a single mapping document: {len(documents)} document(s)"
    return documents[0]


def _resolves(tree: dict[str, Any], path: str) -> bool:
    """Whether every segment of a dotted path exists in the values tree.

    A prefix resolving to a scalar means the rest cannot exist, which is as undefined as a missing key.
    """
    node: Any = tree
    for segment in path.split("."):
        if not isinstance(node, dict) or segment not in node:
            return False
        node = node[segment]
    return True


def _unguarded_unresolved(tree: dict[str, Any], text: str) -> list[tuple[int, str, str]]:
    """Every `.Values` reference in `text` that neither resolves nor sits inside a guard.

    The unit of guarding is the enclosing `{{ ... }}` action: `default` two pipes later still protects
    the interpolation, while a `default` in a neighbouring action does not.
    """
    findings: list[tuple[int, str, str]] = []
    for match in _REFERENCE.finditer(text):
        path = match.group(1).lstrip(".")
        if _resolves(tree, path):
            continue
        opened = text.rfind("{{", 0, match.start())
        closed = text.find("}}", match.start())
        action = text[opened:closed] if opened != -1 and closed != -1 else ""
        if _GUARD.search(action):
            continue
        findings.append((text[: match.start()].count("\n") + 1, path, action.strip()))
    return findings


def test_every_bare_values_reference_resolves_in_the_values_tree() -> None:
    """THE GATE. A bare reference to a path nothing defines interpolates `%!s(<nil>)` into a manifest."""
    tree = _values_tree()
    sources = sorted(_TEMPLATES.rglob("*.tpl")) + sorted(_TEMPLATES.rglob("*.yaml"))
    assert sources, "no chart templates were read — this gate would pass vacuously"

    offenders = [
        f"{path.relative_to(REPO)}:{line} — .Values.{ref}\n      {action}"
        for path in sources
        for line, ref, action in _unguarded_unresolved(tree, path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "these templates interpolate a `.Values` path that chart/values.yaml does not define, with no "
        "`default`/`with`/`if`/`required` to catch it — Go renders each as the literal `%!s(<nil>)`, so "
        "the manifest is well-formed and the value is a nil artifact:\n\n" + "\n".join(offenders)
    )


def test_the_scan_catches_the_defect_it_was_written_for() -> None:
    """NON-VACUITY, proven rather than assumed.

    A scanner whose regex stopped matching would report a clean chart forever. It is checked against
    the exact line that shipped, so the gate's own silence has to be earned.
    """
    findings = _unguarded_unresolved(_values_tree(), _THE_DEFECT)

    assert [ref for _, ref, _ in findings] == ["dapr.appApiToken"], (
        f"the scan did not flag the line that shipped the defect, so it is measuring nothing: {findings}"
    )


def test_a_guarded_reference_to_the_same_missing_path_is_accepted() -> None:
    """The other half of the discrimination: guarded is safe, and the gate must not cry wolf.

    Without this, tightening the gate until it rejects everything would look like success.
    """
    tree = _values_tree()

    assert not _unguarded_unresolved(tree, '{{ .Values.dapr.appApiToken | default "fallback" }}'), (
        "`default` supplies a value; nothing nil reaches the manifest"
    )
    assert not _unguarded_unresolved(tree, "{{- with .Values.dapr.appApiToken }}x{{- end }}"), "`with` skips the block entirely when the path is absent"
    assert not _unguarded_unresolved(tree, '{{- required "must be set" .Values.dapr.appApiToken -}}'), (
        "`required` fails the render loudly, which is fail-closed and therefore safe"
    )
