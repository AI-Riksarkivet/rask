"""The flows wire contract — every model the service accepts, returns, or persists in a workflow.

One module for all of them, deliberately: the Dapr Workflow lane serializes activity input and
output through this file, so a type that lives elsewhere is a type whose serialized shape nobody
owns. Pydantic v2 throughout (no dataclasses) — the frontend parses these bodies, and the catalog
response shape in particular is PINNED (see `catalog.py`).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


#: A node's `output_text` is truncated to this many characters before it leaves the service.
#: A run is a sandbox probe, not a transport: a model node can return a whole page of ALTO, and
#: three of those in one RunState is a response nobody reads and a workflow history nobody wants
#: to replay. The frontend re-fetches the full payload from its own inference call when it needs it.
MAX_OUTPUT_CHARS = 4000

#: The ceiling on `NodeResult.payload_text` — the value the GRAPH carries downstream, two orders of
#: magnitude above the display cap because it is a document, not a preview.
#:
#: Uncapped it was an amplifier, not just a large field: in the durable lane the payload is written
#: to the workflow history as the activity's output AND again as the input of every dependent node,
#: so one big node output costs O(dependents) writes to the actor state store. The estate already
#: refuses to move data of that shape through Dapr at all — `service_kit.dapr_publish` caps a
#: published event at 900 KiB on the claim-check rule (events carry pointers, never data).
#:
#: 256 KiB exactly matches `executor._REGEX_MAX_SUBJECT`, the other caller-facing bound on a payload
#: in this service, so a payload that survives this cap is still one a `regex` node will accept —
#: two different ceilings would make "too large" mean two different things one edge apart.
MAX_PAYLOAD_CHARS = 256 * 1024

#: Appended to a payload this service cut, so a downstream node's input SAYS it is incomplete rather
#: than reading as a short document. Formatted with the original length and the ceiling: those two
#: numbers are what turn "the answer looks wrong" into "the payload was 3 MiB".
PAYLOAD_TRUNCATED_MARKER = "\n…[flows truncated this payload: {total} characters exceeded the {cap}-character ceiling]"

#: The two STRUCTURAL bounds. `MAX_PAYLOAD_CHARS` caps one payload; these cap how many of them a
#: single run can put on the wire and in the durable history. Without them the payload cap bounded
#: nothing in aggregate — the ceiling on a run was the ceiling on a drawing.
#:
#: `MAX_NODE_FAN_IN` is the sharp one, and it is reachable with a two-node graph. `workflow.py`
#: builds each activity input as `inputs=[outputs[u] for u in incoming[node_id]]`, so ONE `NodeJob`
#: carries one full payload per incoming EDGE. Counting edges rather than distinct sources is
#: deliberate: `graph.upstreams` appends per edge and `validate_graph` tolerates duplicate edges on
#: purpose (a drag-created duplicate is easy to produce on a canvas), so one upstream dragged N times
#: puts N payloads in one message while presenting a single source.
#:
#: THE ARITHMETIC, which `test_the_bounds_are_ARITHMETICALLY_consistent_...` re-checks so it cannot
#: drift: worst case is 8 x 256 KiB = 2 MiB raw, against a **4 MiB** ceiling, leaving a 2x margin for
#: the node config, the serve origin, the JSON envelope and above all escape expansion, which can
#: nearly double a text payload full of quotes and newlines.
#:
#: THE CEILING IS 4 MiB, NOT daprd's `--max-body-size=32Mi`. This was shipped wrong once (fan-in 32,
#: i.e. 8 MiB — twice the real limit, so the bound did not bind). Two different channels exist and the
#: SMALLER one governs: `--max-body-size` configures daprd's own server, but an activity payload
#: crosses the app<->sidecar WORKFLOW channel opened by the vendored durabletask worker.
#: `WorkflowRuntime` builds `TaskHubGrpcWorker(...)` WITHOUT `channel_options`, so `get_grpc_channel`
#: merges only `DEFAULT_GRPC_KEEPALIVE_OPTIONS` — keepalive values and no `grpc.max_*_message_length`
#: whatsoever — leaving grpc's 4 MiB default. Read out of dapr 1.18.3, and independently reproduced
#: against a real grpc server (`RESOURCE_EXHAUSTED ... 5242880 vs 4194304`). Raising daprd's flag does
#: NOT move it, and nothing plumbs channel options through, so it is not reachable from config at all.
#:
#: Past the limit the worker REJECTS the activity input and the run wedges with nothing to paint,
#: which is why this is refused at validate time as a 422 naming the node.
#:
#: Both numbers are DEFAULTS chosen from that arithmetic, not from a profile of real flows — a
#: visual builder graph does not approach either. Raising one means redoing the sum.
MAX_GRAPH_NODES = 256
MAX_NODE_FAN_IN = 8

#: Terminal-or-running vocabulary, shared by a run and by each of its nodes so a caller reads one
#: set of words. A node that never got to run because an upstream failed is `failed` with the
#: error "upstream failed" — not a fourth state, because "blocked" is a *reason*, not an outcome.
RunStatus = Literal["running", "succeeded", "failed"]


class FlowNode(BaseModel):
    """One node of the drawn graph. `kind` selects the dispatch; `config` is that kind's params."""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: str
    config: dict[str, object] = Field(default_factory=dict)


class FlowEdge(BaseModel):
    """A directed payload edge. The handle ids are carried but not interpreted in v0 —
    dispatch is per-kind and takes all upstream payloads in edge order."""

    model_config = ConfigDict(extra="ignore")

    source: str
    target: str
    source_handle: str | None = None
    target_handle: str | None = None


class FlowGraph(BaseModel):
    #: `forbid`, unlike the node/edge models above — and for a measured reason. Both fields default
    #: to empty, so under `ignore` ANY wrong-shaped body parsed as a valid empty graph and
    #: `/validate` answered `{"ok": true, "problems": []}` — a false all-clear, the worst possible
    #: reply from a validator. The mistake is not hypothetical: `/validate` takes a bare graph while
    #: `/runs` takes `{graph, seeds}`, so posting the wrapped shape to `/validate` is the obvious
    #: slip, and it is exactly what happened the first time this endpoint was driven by hand. Now it
    #: is a 422 naming the unexpected key. The nodes and edges keep `ignore` deliberately: a drawn
    #: node carries editor-only fields (position, label) that the server has no business rejecting.
    model_config = ConfigDict(extra="forbid")

    nodes: list[FlowNode] = Field(default_factory=list)
    edges: list[FlowEdge] = Field(default_factory=list)


class ParamSpec(BaseModel):
    """One configurable field of a node kind, as the palette should render it."""

    name: str
    label: str
    type: Literal["string", "number", "boolean", "select"]
    required: bool = False
    #: Only meaningful for `type == "select"`. Empty means "the client supplies the options" —
    #: which is the case for a model node's `app`, whose options are the LIVE Serve deployments
    #: read from `/api/serve/applications/`. The server cannot enumerate them without reaching
    #: the cluster, and a catalog that needed the cluster to answer would be useless offline.
    options: list[str] = Field(default_factory=list)


class NodeSpec(BaseModel):
    """A node kind as the server declares it (graphbook's model: the catalog is the seam that
    lets a server-side node kind appear in the palette without a frontend release)."""

    kind: str
    label: str
    role: Literal["source", "transform", "sink"]
    params: list[ParamSpec] = Field(default_factory=list)


class CatalogResponse(BaseModel):
    """PINNED SHAPE — the frontend parses this: `{"version": 1, "kinds": [NodeSpec, ...]}`.

    `version` is an integer the client can branch on, not the service version. Bump it only when
    the *shape* changes incompatibly; adding a kind or a param is not a bump.
    """

    version: Literal[1] = 1
    kinds: list[NodeSpec]


class ValidateResponse(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list)


class RunRefused(BaseModel):
    """The 422 body for a run whose graph does not validate — RFC 9457 problem+json plus the
    structured `problems` list (an extension member, which the RFC explicitly allows).

    The list is the point: the builder highlights the offending nodes, and it cannot do that from a
    single flattened `detail` string, which is all `service_kit.exceptions._problem` produces.
    """

    type: str = "about:blank#flow-invalid"
    title: str = "Unprocessable Entity"
    status: int = 422
    detail: str
    problems: list[str] = Field(default_factory=list)


class RunRequest(BaseModel):
    """A run: the graph, plus the text payload for each source node.

    `seeds` is keyed by NODE ID and holds text only. Bytes inputs (the `image` node kind) are a
    FRONTEND-ONLY lane in v0: an image rides the studio zone's own `+server.ts` straight to Serve
    because base64-ing it through a JSON envelope costs a third more bytes and triple-buffers it
    (the estate's transport ruling). So a server-side run that reaches an `image` node fails that
    node by design — see `executor.dispatch` — rather than pretending to have data it never got.
    """

    graph: FlowGraph
    seeds: dict[str, str] = Field(default_factory=dict)


class RunJob(RunRequest):
    """The DURABLE lane's input contract: a run request plus the resolved Serve origin.

    Separate from `RunRequest` because the client does not get to choose which cluster a run talks
    to — the service resolves that from its own settings at schedule time and the workflow carries
    the resolved value, for the same reason `NodeJob` does: an activity may execute on any worker
    after any replay, and re-reading the origin from that worker's environment is how one run ends
    up half-executed against two clusters.
    """

    serve_url: str
    serve_timeout: float = 180.0


class NodeRunState(BaseModel):
    """The outcome of one node: what the builder paints on it (status colour, timing, output)."""

    status: RunStatus
    #: Wall time for THIS node only, milliseconds. None when the node never executed.
    ms: float | None = None
    output_text: str | None = None
    error: str | None = None

    @field_validator("output_text")
    @classmethod
    def _truncate(cls, value: str | None) -> str | None:
        # Enforced on the MODEL, not at each construction site: the executor, the activity and the
        # workflow all build these, and a cap that lives in three places is a cap that holds in two.
        if value is not None and len(value) > MAX_OUTPUT_CHARS:
            return value[:MAX_OUTPUT_CHARS]
        return value


class RunState(BaseModel):
    """A whole run, addressable at `GET /flows/runs/{run_id}`."""

    run_id: str
    status: RunStatus
    nodes: dict[str, NodeRunState] = Field(default_factory=dict)
    error: str | None = None


class NodeResult(BaseModel):
    """What one node's execution produced — and the Dapr activity's OUTPUT contract.

    Two fields because they answer two questions, and the two carry DIFFERENT ceilings. `state` is
    what the builder paints, capped at ``MAX_OUTPUT_CHARS`` (4 000) because it is a preview.
    `payload_text` is what the GRAPH carries to the dependent nodes, capped at ``MAX_PAYLOAD_CHARS``
    (256 KiB) because it is a document — a page of ALTO exceeds the display cap routinely, and
    feeding the truncated PREVIEW downstream would hand an `alto` node a document cut mid-element.

    The graph cap is loud, which is the whole reason it can exist at all: a truncated payload ends
    with ``PAYLOAD_TRUNCATED_MARKER`` naming the original length, so a document cut mid-element
    announces itself in the dependent node's own input instead of producing a plausible short
    answer. A silent truncation here would be worse than no cap; a marked one is not.
    """

    state: NodeRunState
    payload_text: str | None = None

    @field_validator("payload_text")
    @classmethod
    def _cap(cls, value: str | None) -> str | None:
        # On the MODEL for the same reason `NodeRunState._truncate` is: the executor, the activity
        # and the workflow all build these, and this is the exact value that lands in the durable
        # workflow history — once as this activity's output, then again as every dependent's input.
        if value is None or len(value) <= MAX_PAYLOAD_CHARS:
            return value
        marker = PAYLOAD_TRUNCATED_MARKER.format(total=len(value), cap=MAX_PAYLOAD_CHARS)
        # The marker rides INSIDE the ceiling, not past it: the cap is what a dependent node
        # receives, and a bound that its own input check (`_REGEX_MAX_SUBJECT`) then rejects by a
        # few dozen characters would be a bound that fails at exactly the size it was set for.
        return value[: MAX_PAYLOAD_CHARS - len(marker)] + marker


class Payload(BaseModel):
    """What travels along an edge.

    `kind` exists so the union the frontend already models (`{kind:'bytes'|'text'}`) has a
    server-side counterpart, but v0 only ever produces `text`: the one binary source is the
    `image` node, which is refused server-side (see `RunRequest`). Frozen — a payload is a value.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["text", "bytes"] = "text"
    text: str = ""

    def as_bytes(self) -> bytes:
        return self.text.encode("utf-8")


class NodeJob(BaseModel):
    """One node's execution request — and the Dapr activity's input contract.

    Self-contained on purpose. An activity may run on any worker, after any replay, so it carries
    the resolved Serve origin rather than re-reading it from that worker's environment: two
    derivations of one address is how a run ends up split across two clusters.
    """

    node: FlowNode
    #: Upstream payload texts, in edge order.
    inputs: list[str] = Field(default_factory=list)
    seed: str | None = None
    serve_url: str
    #: Per-call budget in seconds, carried for the same reason as the origin.
    serve_timeout: float = 180.0
