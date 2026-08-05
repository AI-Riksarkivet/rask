"""The pure graph layer: hygiene problems, topological order, topological waves."""

import pytest
from pydantic import ValidationError

from flows.graph import topo_order, topo_waves, upstreams, validate_graph
from flows.models import FlowEdge, FlowGraph, FlowNode


def _graph(nodes: list[tuple[str, str]], edges: list[tuple[str, str]]) -> FlowGraph:
    return FlowGraph(
        nodes=[FlowNode(id=i, kind=k) for i, k in nodes],
        edges=[FlowEdge(source=s, target=t) for s, t in edges],
    )


def test_a_linear_graph_has_no_problems() -> None:
    graph = _graph([("a", "text"), ("b", "alto"), ("c", "inspect")], [("a", "b"), ("b", "c")])
    assert validate_graph(graph) == []


def test_duplicate_node_id_is_a_problem() -> None:
    graph = _graph([("a", "text"), ("a", "inspect")], [])
    assert validate_graph(graph) == ["duplicate node id: a"]


def test_unknown_kind_is_a_problem_and_names_the_node() -> None:
    graph = _graph([("a", "quantum")], [])
    problems = validate_graph(graph)
    assert problems == ["unknown node kind: quantum (node a)"]


def test_dangling_edge_endpoints_are_problems() -> None:
    graph = _graph([("a", "text")], [("a", "ghost"), ("nowhere", "a")])
    problems = validate_graph(graph)
    assert "edge target names no node: ghost" in problems
    assert "edge source names no node: nowhere" in problems


def test_self_loop_is_named_and_suppresses_the_generic_cycle_line() -> None:
    """A self-loop IS a cycle. The specific message says which node to fix; the generic one does
    not, so reporting both would be noise the user has to read twice."""
    graph = _graph([("a", "text")], [("a", "a")])
    assert validate_graph(graph) == ["self-loop on node: a"]


def test_a_cycle_is_refused() -> None:
    graph = _graph([("a", "text"), ("b", "alto"), ("c", "inspect")], [("a", "b"), ("b", "c"), ("c", "a")])
    assert validate_graph(graph) == ["graph has a cycle"]
    assert topo_order(graph) is None
    assert topo_waves(graph) is None


def test_topo_order_respects_dependencies() -> None:
    graph = _graph([("c", "inspect"), ("a", "text"), ("b", "alto")], [("a", "b"), ("b", "c")])
    assert topo_order(graph) == ["a", "b", "c"]


def test_topo_waves_group_independent_nodes() -> None:
    # a -> c, b -> c: a and b are independent and must share a wave, or two model calls that could
    # run at once would cost the sum of their latencies.
    graph = _graph([("a", "text"), ("b", "text"), ("c", "inspect")], [("a", "c"), ("b", "c")])
    assert topo_waves(graph) == [["a", "b"], ["c"]]


def test_topo_waves_are_sorted_so_the_plan_is_replay_identical() -> None:
    """The durable orchestrator recomputes the plan on every replay instead of persisting it. That is
    only safe if the plan is a pure function of the graph — hence sorted waves, not set order."""
    forward = _graph([("z", "text"), ("m", "text"), ("a", "text")], [])
    backward = _graph([("a", "text"), ("m", "text"), ("z", "text")], [])
    assert topo_waves(forward) == topo_waves(backward) == [["a", "m", "z"]]


def test_a_duplicated_edge_does_not_invent_a_cycle() -> None:
    """Counting one dependency twice leaves the target permanently un-ready — a Kahn implementation
    reports that as a cycle, and a drag-created duplicate edge is easy to produce in a canvas."""
    graph = FlowGraph(
        nodes=[FlowNode(id="a", kind="text"), FlowNode(id="b", kind="inspect")],
        edges=[FlowEdge(source="a", target="b"), FlowEdge(source="a", target="b")],
    )
    assert validate_graph(graph) == []
    assert topo_waves(graph) == [["a"], ["b"]]


def test_upstreams_preserve_edge_order() -> None:
    graph = _graph([("a", "text"), ("b", "text"), ("c", "inspect")], [("b", "c"), ("a", "c")])
    assert upstreams(graph)["c"] == ["b", "a"]


def test_an_empty_graph_is_REFUSED_and_has_no_waves() -> None:
    """An empty graph is not clean, it is nothing to run.

    This used to assert `validate_graph(empty) == []`, and that reading is what let a wrong-shaped
    request body pass validation: both `FlowGraph` fields default to empty, so any body the model
    tolerated became an empty graph and `/validate` answered `{"ok": true, "problems": []}` — a false
    all-clear. `FlowGraph` forbids unknown keys now, and an empty graph names itself as the problem.
    `topo_waves` still answers `[]` — there is genuinely nothing to schedule, and it is called BY
    `validate_graph`, so it must stay total over the graphs that function rejects.
    """
    empty = FlowGraph()
    assert validate_graph(empty) == ["graph has no nodes"]
    assert topo_waves(empty) == []


def test_a_wrapped_graph_is_REFUSED_rather_than_read_as_empty() -> None:
    """`/validate` takes a bare graph while `/runs` takes `{graph, seeds}` — so posting the wrapped
    shape to `/validate` is the obvious slip, and under `extra="ignore"` it silently validated as a
    clean empty graph. The unknown key is a hard error now."""
    with pytest.raises(ValidationError):
        FlowGraph.model_validate({"graph": {"nodes": [{"id": "a", "kind": "text"}], "edges": []}})


def test_a_node_still_tolerates_editor_only_fields() -> None:
    """The forbid is on the GRAPH, not its members: a drawn node carries `position`/`label` the
    server has no business rejecting, so nodes and edges keep `extra="ignore"`."""
    graph = FlowGraph.model_validate(
        {
            "nodes": [{"id": "a", "kind": "text", "position": {"x": 1, "y": 2}, "label": "mine"}],
            "edges": [{"source": "a", "target": "a", "animated": True}],
        }
    )
    assert [n.id for n in graph.nodes] == ["a"]
