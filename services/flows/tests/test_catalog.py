"""The catalog's SHAPE, pinned. The frontend parses this body, so a rename here is a break there."""

from flows.catalog import CATALOG, KNOWN_KINDS
from flows.models import CatalogResponse


def test_the_response_shape_is_version_plus_kinds() -> None:
    body = CatalogResponse(kinds=CATALOG).model_dump()
    assert set(body) == {"version", "kinds"}
    assert body["version"] == 1


def test_every_kind_is_declared_with_its_role() -> None:
    """EXACT, both directions: a kind the frontend can draw but this service does not declare is
    refused by `validate_graph` as "unknown", so the editor's palette and this list must agree."""
    roles = {spec.kind: spec.role for spec in CATALOG}
    assert roles == {
        "image": "source",
        "text": "source",
        "dataset": "source",
        "prompt": "transform",
        "model": "transform",
        "mcp": "transform",
        "alto": "transform",
        "extract": "transform",
        "regex": "transform",
        "compare": "sink",
        "inspect": "sink",
    }


def test_a_node_spec_carries_kind_label_role_params() -> None:
    spec = next(s for s in CATALOG if s.kind == "model")
    assert set(spec.model_dump()) == {"kind", "label", "role", "params"}


def test_a_param_spec_carries_name_label_type_required_options() -> None:
    spec = next(s for s in CATALOG if s.kind == "model")
    app = next(p for p in spec.params if p.name == "app")
    assert set(app.model_dump()) == {"name", "label", "type", "required", "options"}
    assert app.type == "select"
    assert app.required is True
    # Deliberately empty: only the cluster knows which Serve apps are live, and a catalog that had to
    # reach the cluster to answer would be useless offline. The client fills these from /api/serve.
    assert app.options == []


def test_known_kinds_is_derived_from_the_catalog() -> None:
    assert {spec.kind for spec in CATALOG} == KNOWN_KINDS
