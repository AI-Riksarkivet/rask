"""No rendered document carries a mapping with the same key twice.

A duplicate key is not a YAML error: the parser keeps the LAST one and drops the rest, silently. So a
template that grows a second `env:` on a container renders valid YAML, passes every schema check, and
ships a container missing the environment the first block was added to give it.

MEASURED, because this is how the gate was written: hardening five one-shot Jobs added an `env:` block
to three containers that already had one. The render succeeded, the manifests were valid, and
`HOME=/tmp` was dropped on the floor — so `mc` tried to write its config to `/.mc` on a read-only
rootfs and the pre-upgrade hook failed the whole `helm upgrade`. Nothing between the edit and the
cluster could see it.

PyYAML PERMITS DUPLICATES BY DEFAULT, which is why this needs a loader rather than an assertion: the
check has to run while the mapping is being built, since by the time `safe_load` returns, the evidence
is gone.
"""

from __future__ import annotations

import pytest
import yaml
import yaml.constructor
import yaml.resolver
from test_invariants import _helm_template


class _DuplicateKeyLoader(yaml.SafeLoader):
    """A SafeLoader that refuses a mapping naming the same key twice."""


def _no_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            mark = key_node.start_mark
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r} at line {mark.line + 1}, column {mark.column + 1}", node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_DuplicateKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def test_the_render_produces_documents() -> None:
    """Without this the walk below passes by parsing nothing."""
    docs = [d for d in yaml.safe_load_all(_helm_template()) if isinstance(d, dict)]
    assert len(docs) > 50, f"only {len(docs)} documents rendered — the render or this walk is broken"


def test_no_rendered_document_names_a_key_twice() -> None:
    """The whole chart, every document. A duplicate key silently keeps the last value and drops the rest."""
    offenders: list[str] = []
    for chunk in _helm_template().split("\n---\n"):
        if not chunk.strip():
            continue
        try:
            yaml.load(chunk, Loader=_DuplicateKeyLoader)  # noqa: S506 — a SafeLoader subclass
        except yaml.constructor.ConstructorError as exc:
            source = next((line for line in chunk.splitlines() if line.startswith("# Source:")), "<unknown source>")
            offenders.append(f"{source}: {exc.problem}")
        except yaml.YAMLError:
            continue  # a parse failure is a different gate's subject

    assert not offenders, "rendered documents carry a duplicate mapping key (the last value wins, the rest vanish):\n" + "\n".join(offenders)


@pytest.mark.parametrize("snippet", ["a: 1\na: 2\n", "spec:\n  env:\n    - x\n  env:\n    - y\n"])
def test_the_loader_actually_refuses_a_duplicate(snippet: str) -> None:
    """A gate that cannot fail proves nothing — this one is checked against a duplicate it must reject."""
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load(snippet, Loader=_DuplicateKeyLoader)  # noqa: S506 — a SafeLoader subclass
