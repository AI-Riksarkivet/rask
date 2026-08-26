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


# --- Size bounds: what stops one run from writing an unbounded workflow history ------------------


def test_a_graph_larger_than_the_node_cap_is_REFUSED() -> None:
    """The durable lane persists every node's payload to the actor state store, so an unbounded
    node count is an unbounded history — and history is RELOADED on every replay.

    `MAX_PAYLOAD_CHARS` already bounds ONE payload (256 KiB). Nothing bounded how many of them a
    single run could produce, so the ceiling on a run was the ceiling on a drawing.
    """
    from flows.models import MAX_GRAPH_NODES

    ok = _graph([(f"n{i}", "text") for i in range(MAX_GRAPH_NODES)], [])
    assert validate_graph(ok) == [], "a graph exactly AT the cap must run — an off-by-one here refuses legitimate work"

    # TWO BOUNDS NOW, and the order matters. Since 2026-08-26 `FlowGraph.nodes` carries `max_length`,
    # so an over-ceiling graph is refused by pydantic BEFORE any node is built — the HTTP door never
    # reaches this function with one. That is the fix for the unmetered parse (measured: 500,000 nodes
    # = 23.26 MiB = 3.00 s of event-loop block, spent only to answer "over the ceiling").
    with pytest.raises(ValidationError):
        _graph([(f"n{i}", "text") for i in range(MAX_GRAPH_NODES + 1)], [])

    # …and the check HERE stays, exercised through `model_construct`, which is the one way to build a
    # FlowGraph without validation. It is defence in depth for a programmatic caller, and it is what
    # states the problem in the caller's own vocabulary alongside the other hygiene problems — a 422
    # cannot do that.
    too_big = FlowGraph.model_construct(nodes=[FlowNode(id=f"n{i}", kind="text") for i in range(MAX_GRAPH_NODES + 1)], edges=[])
    problems = validate_graph(too_big)
    assert problems, f"a {MAX_GRAPH_NODES + 1}-node graph was accepted — the history it writes is bounded by nothing"
    assert any(str(MAX_GRAPH_NODES) in p for p in problems), f"the refusal must name the ceiling, so the caller knows what to cut to: {problems}"


def test_a_node_with_too_many_INCOMING_EDGES_is_refused_and_NAMED() -> None:
    """The sharp cliff, and the one reachable with a small graph.

    `workflow.py` builds each activity input as `inputs=[outputs[u] for u in incoming[node_id]]`, so
    ONE `NodeJob` carries one full payload per incoming edge. At `MAX_PAYLOAD_CHARS` (256 KiB) each
    and daprd's `--max-body-size` of 32Mi (verified live on daprd 1.18.1, where that one flag governs
    HTTP *and* gRPC), 128 upstreams sits exactly on the limit — and JSON escaping pushes a text
    payload past its raw size, so the true cliff is lower and depends on the CONTENT.

    Past it the sidecar rejects the activity input and the run wedges rather than failing with a
    problem the builder can paint. Refusing at validate time is the difference between a 422 naming
    the node and a run that stops with nothing to show.
    """
    from flows.models import MAX_NODE_FAN_IN

    sources = [(f"s{i}", "text") for i in range(MAX_NODE_FAN_IN + 1)]
    graph = _graph([*sources, ("sink", "inspect")], [(f"s{i}", "sink") for i in range(MAX_NODE_FAN_IN + 1)])

    problems = validate_graph(graph)
    assert problems, f"a node with {MAX_NODE_FAN_IN + 1} upstreams was accepted — its activity input is bounded by nothing"
    assert any("sink" in p for p in problems), f"the refusal must NAME the node so the builder can paint it: {problems}"


def test_the_fan_in_bound_counts_EDGES_not_distinct_sources() -> None:
    """The amplifier case, and the reason this bound cannot be written against distinct sources.

    `upstreams` APPENDS per edge (`graph.py`), so a duplicated edge contributes a SECOND copy of the
    same payload to the same `NodeJob`. `validate_graph` tolerates duplicate edges on purpose — the
    suite's own `test_a_duplicated_edge_does_not_invent_a_cycle` pins that, noting a drag-created
    duplicate is easy to produce on a canvas. So the cheapest way to blow the message limit is one
    upstream dragged repeatedly, which a distinct-source count would wave straight through.
    """
    from flows.models import MAX_NODE_FAN_IN

    graph = FlowGraph(
        nodes=[FlowNode(id="a", kind="text"), FlowNode(id="b", kind="inspect")],
        edges=[FlowEdge(source="a", target="b") for _ in range(MAX_NODE_FAN_IN + 1)],
    )

    problems = validate_graph(graph)
    assert problems, (
        f"{MAX_NODE_FAN_IN + 1} copies of ONE edge were accepted: two distinct sources, but "
        f"{MAX_NODE_FAN_IN + 1} payloads in the activity input. Counting distinct sources misses this."
    )
    assert any("b" in p for p in problems), f"the refusal must name the target node: {problems}"


def test_the_bounds_are_ARITHMETICALLY_consistent_with_the_payload_cap_and_the_sidecar_limit() -> None:
    """A bound whose arithmetic nobody checked is a bound that drifts.

    Worst case for one activity input is `MAX_NODE_FAN_IN * MAX_PAYLOAD_CHARS`. That must stay
    comfortably under daprd's 32Mi `--max-body-size`, with room for the node config, the serve
    origin, JSON envelope overhead and — the one that bites — escape expansion, which can nearly
    double a text payload full of quotes and newlines.

    This test fails if someone raises the fan-in cap or the payload cap without redoing that sum.
    """
    from flows.models import MAX_NODE_FAN_IN, MAX_PAYLOAD_CHARS

    # 4 MiB, and NOT daprd's `--max-body-size=32Mi`. Two different channels, and the SMALLER binds.
    # `--max-body-size` governs daprd's own server; the activity payload crosses the app<->sidecar
    # WORKFLOW channel, which the vendored durabletask worker opens. `WorkflowRuntime` constructs
    # `TaskHubGrpcWorker(...)` without `channel_options`, so `get_grpc_channel` merges only
    # `DEFAULT_GRPC_KEEPALIVE_OPTIONS` — keepalive settings and no `grpc.max_*_message_length` at all,
    # leaving grpc's 4 MiB default. Read out of dapr 1.18.3 in `.venv`, and independently reproduced
    # against a real grpc server (`RESOURCE_EXHAUSTED ... 5242880 vs 4194304`, docs/architecture/medallion-cascade.md).
    # It is not raisable from config: nothing in the runtime plumbs channel options through.
    grpc_default_max_message_bytes = 4 * 1024 * 1024
    worst_case_raw = MAX_NODE_FAN_IN * MAX_PAYLOAD_CHARS

    assert worst_case_raw * 2 <= grpc_default_max_message_bytes, (
        f"worst-case activity input is {MAX_NODE_FAN_IN} x {MAX_PAYLOAD_CHARS} = {worst_case_raw} bytes raw, "
        f"which does not leave a 2x margin for JSON escape expansion under the "
        f"{grpc_default_max_message_bytes}-byte workflow-channel limit. Lower MAX_NODE_FAN_IN or "
        f"MAX_PAYLOAD_CHARS — raising daprd's --max-body-size does NOT help, because this is the worker's "
        f"own channel. Past the limit the worker rejects the input and the run WEDGES rather than failing."
    )
