"""lineage-kit's config claims to follow a pattern `packages/storage` does not have (PS-07).

The module docstring read: "Follows the repo convention (`RASK_*` env vars, ``AliasChoices`` so the
official client's own variable names keep working — the same pattern as ``RASK_S3_ENDPOINT_URL`` in
``packages/storage``)". `packages/storage` imports no pydantic-settings at all: it hand-rolls the
precedence with `os.getenv` over three name tuples. Citing it as the exemplar sends the next author
there to copy something that is not written.

A false citation is not a small thing here — this is the file a new settings class gets copied from.
"""

from __future__ import annotations

from pathlib import Path


_REPO = Path(__file__).resolve().parents[3]
_STORAGE_SRC = _REPO / "packages" / "storage" / "src" / "storage"
_LINEAGE_CONFIG = _REPO / "packages" / "lineage-kit" / "src" / "lineage_kit" / "config.py"


def test_the_config_docstring_does_not_cite_storage_as_a_pydantic_settings_example() -> None:
    storage_source = "\n".join(path.read_text(encoding="utf-8") for path in _STORAGE_SRC.rglob("*.py"))
    storage_uses_pydantic_settings = "pydantic_settings" in storage_source
    docstring = _LINEAGE_CONFIG.read_text(encoding="utf-8").split('"""')[1].lower()

    if storage_uses_pydantic_settings:
        return  # the citation would be true; nothing to police
    assert "same pattern as" not in docstring, (
        "the docstring cites `packages/storage` as the `AliasChoices` exemplar, but that package "
        "resolves its env by hand (`_env_first` over `_ENDPOINT_ENVS`) and imports no pydantic-settings."
    )
    if "packages/storage" in docstring:
        assert "by hand" in docstring, "storage may be named here only as the counter-example it is, never as the pattern"
