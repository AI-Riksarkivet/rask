"""Pure graph functions: hygiene problems, topological order, topological waves.

NO I/O in this module, and that is load-bearing twice over. It is what lets `POST /flows/validate`
answer without touching a cluster, and it is what lets the Dapr Workflow orchestrator compute its
own execution plan inside a replaying workflow body, where a network call would be a determinism
bug rather than a slow function.

The vocabulary is shared with the frontend executor on purpose (open_studio_flows.md): both refuse
the same graphs for the same stated reasons.
"""

from collections import deque

from flows.catalog import KNOWN_KINDS
from flows.models import MAX_GRAPH_NODES, MAX_NODE_FAN_IN, FlowGraph


def validate_graph(graph: FlowGraph) -> list[str]:
    """Every structural problem with `graph`, as stable human-readable strings.

    An empty list means the graph is runnable. The problems, in the order checked:
    duplicate node ids, unknown kind, an edge endpoint that names no node, a self-loop, and — via
    :func:`topo_order` — a cycle. The cycle check lives here rather than in the route so there is
    ONE entry point a caller has to remember; `topo_order` stays public and separately testable.

    A self-loop is a cycle, so the generic "cycle" line is suppressed when a self-loop was already
    named: the specific message tells the user which node to fix, the generic one does not.
    """
    problems: list[str] = []

    # A graph with no nodes is not "clean", it is nothing to run — and reporting it clean is how an
    # empty request body reads as a passing validation. Named first so the reply says what is wrong
    # rather than listing zero problems.
    if not graph.nodes:
        problems.append("graph has no nodes")

    # SIZE, before shape. The durable lane writes every node's payload to the actor state store and
    # reloads that history on every replay, so an unbounded node count is an unbounded history — the
    # per-payload `MAX_PAYLOAD_CHARS` cap bounds one document, not a run. Named with the ceiling so
    # the caller knows what to cut to rather than bisecting.
    if len(graph.nodes) > MAX_GRAPH_NODES:
        problems.append(f"graph has {len(graph.nodes)} nodes, over the {MAX_GRAPH_NODES}-node ceiling")

    seen: set[str] = set()
    for node in graph.nodes:
        if node.id in seen:
            problems.append(f"duplicate node id: {node.id}")
        seen.add(node.id)
        if node.kind not in KNOWN_KINDS:
            problems.append(f"unknown node kind: {node.kind} (node {node.id})")

    self_loop = False
    for edge in graph.edges:
        if edge.source not in seen:
            problems.append(f"edge source names no node: {edge.source}")
        if edge.target not in seen:
            problems.append(f"edge target names no node: {edge.target}")
        if edge.source == edge.target:
            problems.append(f"self-loop on node: {edge.source}")
            self_loop = True

    # FAN-IN, measured through `upstreams` itself rather than recounted here. That function is what
    # `workflow.py` builds each activity input from, so counting any other way bounds a number that
    # never reaches the wire — and it already drops dangling and self edges, which are reported above
    # as their own problems. Counting EDGES is the point: duplicates are tolerated by design and each
    # one is another full copy of the same payload in the same message.
    for node_id, incoming in upstreams(graph).items():
        if len(incoming) > MAX_NODE_FAN_IN:
            problems.append(f"node {node_id} has {len(incoming)} incoming edges, over the {MAX_NODE_FAN_IN}-edge ceiling")

    if not self_loop and topo_order(graph) is None:
        problems.append("graph has a cycle")

    return problems


def topo_order(graph: FlowGraph) -> list[str] | None:
    """Kahn's algorithm. Node ids in a runnable order, or None if the graph has a cycle.

    Tolerant of the graphs `validate_graph` rejects, because it is called BY that function: edges
    with an endpoint that names no node are ignored, and a duplicate id appears once. A partial
    order for a broken graph is fine — the caller is asking "is there a cycle", and an exception
    here would turn one reportable problem into a 500.
    """
    ids = _node_ids(graph)
    successors, indegree = _adjacency(graph, ids)

    ready = deque(sorted(n for n in ids if indegree[n] == 0))
    order: list[str] = []
    while ready:
        node = ready.popleft()
        order.append(node)
        for nxt in successors[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)

    return order if len(order) == len(ids) else None


def topo_waves(graph: FlowGraph) -> list[list[str]] | None:
    """The same order, grouped into waves that may run CONCURRENTLY: wave *n* depends only on
    waves before it. None if the graph has a cycle.

    Waves rather than a flat order because both executors fan out — the inline one with
    `asyncio.gather`, the durable one with `wf.when_all` — and a flat list would serialize a graph
    whose whole point is that two model nodes can run at once. Each wave's ids are sorted, so the
    plan is byte-identical across processes and across a workflow replay.
    """
    ids = _node_ids(graph)
    successors, indegree = _adjacency(graph, ids)

    waves: list[list[str]] = []
    frontier = sorted(n for n in ids if indegree[n] == 0)
    placed = 0
    while frontier:
        waves.append(frontier)
        placed += len(frontier)
        nxt_wave: list[str] = []
        for node in frontier:
            for nxt in successors[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    nxt_wave.append(nxt)
        frontier = sorted(nxt_wave)

    return waves if placed == len(ids) else None


def upstreams(graph: FlowGraph) -> dict[str, list[str]]:
    """Per node, the source ids of its incoming edges IN EDGE ORDER — the order a node's dispatch
    receives its payloads in. Edges whose endpoints name no node are dropped (see `topo_order`)."""
    ids = _node_ids(graph)
    incoming: dict[str, list[str]] = {n: [] for n in ids}
    for edge in graph.edges:
        if edge.source in ids and edge.target in ids and edge.source != edge.target:
            incoming[edge.target].append(edge.source)
    return incoming


def _node_ids(graph: FlowGraph) -> list[str]:
    # dict.fromkeys, not a set: insertion order is preserved, so a graph with no edges still runs
    # in the order it was drawn instead of an order that depends on hash randomization.
    return list(dict.fromkeys(node.id for node in graph.nodes))


def _adjacency(graph: FlowGraph, ids: list[str]) -> tuple[dict[str, list[str]], dict[str, int]]:
    known = set(ids)
    successors: dict[str, list[str]] = {n: [] for n in ids}
    indegree: dict[str, int] = dict.fromkeys(ids, 0)
    seen_edges: set[tuple[str, str]] = set()
    for edge in graph.edges:
        if edge.source not in known or edge.target not in known:
            continue
        if edge.source == edge.target:
            # A self-loop would make indegree unsatisfiable and every caller would read "cycle",
            # which is true but is reported precisely by validate_graph. Counting it once here
            # keeps that the ONLY message about it.
            indegree[edge.target] += 1
            continue
        if (edge.source, edge.target) in seen_edges:
            # A duplicated edge is not a dependency twice over; counting it twice would leave the
            # target permanently un-ready and report a cycle that does not exist.
            continue
        seen_edges.add((edge.source, edge.target))
        successors[edge.source].append(edge.target)
        indegree[edge.target] += 1
    return successors, indegree
