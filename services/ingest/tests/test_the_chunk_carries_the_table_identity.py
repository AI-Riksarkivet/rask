"""A chunk must carry the NAMESPACE, because vending is keyed on the table id and nothing downstream
can recover it.

The worker holds only `dataset_uri`, and the vending door is keyed `{namespace}${dataset}`. Two ways
to bridge that, and only one is safe:

* Parse the identity back out of the URI. Refused: `rask-lance-catalog` documents FIVE distinct
  dataset-URI layouts, and reducing the wrong one does not error — it silently yields `None` or the
  PARENT namespace. A credential vended for the wrong table is a 403 at write time that reads as a
  permission problem.
* Carry the resolved value, which is what this chunk already does for `dataset_uri` and `sizing` for
  exactly the same reason ("Carried, not re-resolved").

It must be the NAMESPACE and not `project`. `RunSpec.namespace` is "THE ONE PLACE a project becomes a
namespace", and the estate has already paid for confusing them: the catalog client's parameter was
once named `project`, so the mistake type-checked and read correctly at every site while composing
`bind86$e2ewin` — the 403's object, which nobody had granted anything on because `namespace:bind86`
does not exist. Carrying `project` here would rebuild that bug one layer down.

The field DEFAULTS, and that is load-bearing rather than laziness: a chunk enqueued by the previous
build is replayed by the new one — Dapr hands back the recorded input verbatim — so a required field
would fail every in-flight run at the moment of deploy.
"""

from __future__ import annotations


def test_a_chunk_carries_the_resolved_namespace_not_the_project() -> None:
    from ingest.workflow import ChunkSpec

    fields = ChunkSpec.model_fields
    assert "namespace" in fields, "the worker cannot compose a table id without it"
    assert fields["namespace"].default == "", "a replayed pre-upgrade chunk must still validate"


def test_the_dispatcher_fills_it_from_the_ONE_place_that_derives_it() -> None:
    """Not `spec.project`, and not a second copy of the derivation — the value RunSpec resolves.

    AST over the module rather than a string search: `"ChunkSpec("` also matches the class statement,
    and a test that asserts against the model definition instead of the call site passes for the wrong
    reason.
    """
    import ast
    import inspect

    from ingest import workflow

    tree = ast.parse(inspect.getsource(workflow))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ChunkSpec"]
    assert calls, "the dispatcher's ChunkSpec construction was not found"
    supplied = {kw.arg: ast.unparse(kw.value) for call in calls for kw in call.keywords}
    assert supplied.get("namespace") == "spec.namespace", f"the chunk must carry the RESOLVED namespace, got {supplied.get('namespace')!r}"


def test_a_pre_upgrade_chunk_still_validates() -> None:
    """The replay case, driven rather than asserted about: a recorded input from before this field
    existed must round-trip, or deploying strands every run mid-flight."""
    from ingest.workflow import ChunkSpec

    old = {"run_id": "r1", "chunk_id": "r1-c0", "offset": 0, "count": 3, "dataset_uri": "s3://b/t.lance"}
    chunk = ChunkSpec.model_validate(old)
    assert chunk.namespace == ""
