"""Node dispatch and the inline run executor.

Two entry points, one dispatch. `dispatch()` executes ONE node and is shared by both lanes — the
inline `execute()` below and the Dapr activity in `activities.py` — because a node that behaves
differently depending on which orchestrator called it is a node whose sandbox result means nothing.

The HTTP client is a PARAMETER, never built here. That is what makes the Serve boundary testable at
the transport (respx) instead of by patching our own functions, and it is what lets the inline lane
reuse the app-scoped client while an activity gets a short-lived one on its own worker.
"""

import asyncio
import json
import logging
import re
import time
from typing import cast
from xml.sax.saxutils import unescape

import httpx
import regex

from flows.graph import topo_waves, upstreams
from flows.models import (
    FlowGraph,
    FlowNode,
    NodeJob,
    NodeResult,
    NodeRunState,
    Payload,
    RunState,
)


log = logging.getLogger(__name__)


#: A Serve application name, as it appears in the route prefix. Validated rather than trusted: the
#: value comes from a drawn graph, is concatenated into a URL, and `..%2f` or a leading `/` would
#: aim the POST at an arbitrary path on the Serve host. Same alphabet Serve's own app names use.
APP_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: ALTO extraction, regex-based and dependency-free (no lxml in this service's closure — a parser
#: is a heavy dependency to add for a sandbox transform whose input is one page of known-shape XML).
#: Both the paired and the self-closing form of TextLine, so a page of empty lines is not mistaken
#: for a page with no lines at all.
_TEXTLINE_RE = re.compile(r"<TextLine\b[^>]*/>|<TextLine\b.*?</TextLine>", re.DOTALL | re.IGNORECASE)
_STRING_RE = re.compile(r'<String\b[^>]*?\bCONTENT="([^"]*)"', re.DOTALL | re.IGNORECASE)

#: `unescape` handles &amp; &lt; &gt; only; ALTO CONTENT routinely carries the other two.
_XML_ENTITIES = {"&quot;": '"', "&apos;": "'"}


class NodeError(Exception):
    """A node failed for a reason worth showing on the node. The message lands in
    `NodeRunState.error` verbatim, so keep it short, stable and about the GRAPH — never a raw
    upstream exception (log that instead; an exception string is not a user-facing sentence)."""


#: Sentinel for "the path does not exist", so a legitimately-null JSON value is not mistaken for a
#: miss (`None` is a perfectly good value to extract).
_MISSING: object = object()


def pick_path(value: object, path: str) -> object:
    """Walk a dotted path into parsed JSON — ``choices[0].message.content``, ``data.0.embedding``.

    Returns :data:`_MISSING` for any step that does not exist, so a wrong path is a reportable miss
    rather than a traceback. Bracket and dotted indices both work because both spellings occur in the
    wild and guessing wrong is the likeliest user error here. Mirrors the frontend's ``pickPath``.
    """
    cur = value
    for step in (s for s in re.sub(r"\[(\d+)\]", r".\1", path).split(".") if s):
        if isinstance(cur, list):
            if not step.lstrip("-").isdigit():
                return _MISSING
            index = int(step)
            if not -len(cur) <= index < len(cur):
                return _MISSING
            cur = cur[index]
        elif isinstance(cur, dict):
            # `cast`, because narrowing `object` with `isinstance(dict)` yields `dict[Unknown, Unknown]`
            # whose key type is `Never` — ty rejects the subscript. Parsed JSON keys are always str.
            mapping = cast(dict[str, object], cur)
            if step not in mapping:
                return _MISSING
            cur = mapping[step]
        else:
            return _MISSING  # a scalar with path left to walk
    return cur


def compare_texts(texts: list[str]) -> str:
    """Side-by-side report for a Compare node — the A/B affordance, as TEXT so it travels downstream
    like any other payload. Byte-for-byte the frontend's ``compareTexts`` shape, because the same
    graph run in either lane must read the same."""
    lines: list[str] = [f"{len(texts)} inputs compared", ""]
    for i, text in enumerate(texts):
        lines += [f"── {chr(65 + i)} ({len(text)} chars) ──", text, ""]
    if len(texts) >= 2:
        a, b = texts[0], texts[1]
        lines.append("── verdict: A and B are identical ──" if a == b else "── verdict: A and B differ ──")
        if a != b:
            al, bl = a.split("\n"), b.split("\n")
            at = next((i for i, line in enumerate(al) if i >= len(bl) or line != bl[i]), -1)
            if at >= 0:
                lines += [
                    f"first difference at line {at + 1}:",
                    f"A: {al[at] if at < len(al) else ''}",
                    f"B: {bl[at] if at < len(bl) else ''}",
                ]
    return "\n".join(lines)


def alto_lines(xml: str) -> list[str]:
    """The text of an ALTO document, one entry per `<TextLine>`, words joined by spaces.

    Falls back to "every String in the document as a single line" ONLY when the document contains
    no TextLine element at all — a Serve app that returns bare Strings still yields something,
    while a page whose TextLines are genuinely empty correctly yields nothing.
    """
    blocks = _TEXTLINE_RE.findall(xml)
    if blocks:
        lines = []
        for block in blocks:
            words = [unescape(w, _XML_ENTITIES) for w in _STRING_RE.findall(block)]
            text = " ".join(w for w in words if w)
            if text:
                lines.append(text)
        return lines

    words = [unescape(w, _XML_ENTITIES) for w in _STRING_RE.findall(xml)]
    joined = " ".join(w for w in words if w)
    return [joined] if joined else []


async def dispatch(
    node: FlowNode,
    inputs: list[Payload],
    seed: str | None,
    *,
    client: httpx.AsyncClient,
    serve_url: str,
    serve_timeout: float = 180.0,
) -> Payload:
    """Execute one node — a ROUTER, one line per kind.

    Each arm's body lives in its own `_kind` helper below. That is not decoration: with eleven kinds
    inline this function tripped ruff's C901 complexity ceiling (21 > 15), and a router that reads as
    a table is also the thing you scan to answer "which kinds exist".
    """
    match node.kind:
        case "text":
            return _text(node, seed)
        case "image":
            # Stated, not silently skipped. Bytes ride the studio zone's own +server.ts straight to
            # Serve (the estate's transport ruling: devalue would base64 an upload inside the JSON
            # payload, +33% and triple-buffered), so a server-side run never receives the image.
            raise NodeError("image seeds are frontend-only in v0")
        case "dataset":
            return _dataset(node)
        case "prompt":
            return _prompt(node, inputs)
        case "model":
            return await _call_serve(node, inputs, client=client, serve_url=serve_url, serve_timeout=serve_timeout)
        case "mcp":
            return _mcp(node)
        # The pure-CPU arms run OFF the loop: caller-supplied regex (and the DOTALL scans inside
        # alto_lines/compare_texts) are unbounded CPU work, and inline they froze the whole process —
        # probes included, since /api/health shares the loop (open_python-audit
        # FLOWS-REDOS-ON-LOOP). A thread does not stop a GIL stall, so the subject-length cap in
        # `_regex` is the load-bearing half; the threadpool keeps well-behaved work from starving
        # concurrent requests.
        case "alto":
            text = _one_input(node, inputs).text
            return Payload(text="\n".join(await asyncio.to_thread(alto_lines, text)))
        case "extract":
            return await asyncio.to_thread(_extract, node, inputs)
        case "regex":
            return await asyncio.to_thread(_regex, node, inputs)
        case "compare":
            # The MULTI-input kind, so it does NOT go through `_one_input`: comparing one thing is not
            # comparing. Same report shape as the frontend's `compareTexts`.
            if not inputs:
                raise NodeError("compare node has no upstream")
            texts = [p.text for p in inputs]
            return Payload(text=await asyncio.to_thread(compare_texts, texts))
        case "inspect":
            # Passthrough sink: it exists to be READ, so it must not transform. Several upstreams
            # concatenate in edge order rather than one silently winning.
            if not inputs:
                raise NodeError("inspect node has no upstream")
            return Payload(text="\n".join(p.text for p in inputs))
        case _:
            # Unreachable through the routes (validate_graph rejects unknown kinds first), kept so a
            # kind added to the catalog without a dispatch arm fails loudly on ONE node instead of
            # 500-ing the run.
            raise NodeError(f"no dispatch for node kind: {node.kind}")


def _text(node: FlowNode, seed: str | None) -> Payload:
    """The run's seed WINS over the stored config, so one saved graph replays with new input."""
    if seed is not None:
        return Payload(text=seed)
    configured = node.config.get("text")
    return Payload(text=configured if isinstance(configured, str) else "")


def _dataset(node: FlowNode) -> Payload:
    """SCAFFOLD, and refusing is the honest implementation: reading a governed table means the Arrow
    lane (`/api/explorer/**`), and emitting plausible rows here would let a flow read as working when
    it never touched the lakehouse. The frontend's `dataset` case refuses with the same reasoning."""
    table = node.config.get("table")
    named = f" from {table}" if isinstance(table, str) and table else ""
    raise NodeError(f"dataset source is a scaffold — reading rows{named} needs the Arrow lane")


def _mcp(node: FlowNode) -> Payload:
    """SCAFFOLD, refused for the same reason `dataset` is: MCP servers in this repo are Claude Code's
    SESSION tooling (`make claude-bootstrap`), not something the fleet can reach, so calling one needs
    an MCP client plus a stdio/SSE transport that does not exist."""
    parts = (node.config.get("mcpServer"), node.config.get("mcpTool"))
    tool = "/".join(p for p in parts if isinstance(p, str) and p)
    raise NodeError(f"MCP tool node is a scaffold — calling {tool or 'a tool'} needs a server-side MCP client")


def _prompt(node: FlowNode, inputs: list[Payload]) -> Payload:
    """Pure composition, identical to the frontend's arm: `{{input}}` verbatim, no escaping — this
    produces prose for a model to read, not a JSON body."""
    template = node.config.get("promptTemplate")
    if not isinstance(template, str) or not template.strip():
        raise NodeError("prompt node has no template")
    return Payload(text=template.replace("{{input}}", "\n".join(p.text for p in inputs)))


def _extract(node: FlowNode, inputs: list[Payload]) -> Payload:
    """Pull one field out of an upstream JSON envelope by dotted path — the node that makes a model's
    answer chainable. A string comes out UNQUOTED; anything else is re-serialised."""
    payload = _one_input(node, inputs)
    path = node.config.get("extractPath")
    try:
        parsed = json.loads(payload.text)
    except json.JSONDecodeError as exc:
        raise NodeError("upstream is not JSON — nothing to extract a path from") from exc
    value = pick_path(parsed, path) if isinstance(path, str) and path.strip() else parsed
    if value is _MISSING:
        raise NodeError(f'no value at "{path}" in the upstream JSON')
    return Payload(text=value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False))


#: The regex arm's subject-length cap. It bounds MEMORY and nothing else — the comment here used to
#: claim it was the load-bearing defence against backtracking, reasoning that a nested-quantifier
#: pattern is "O(2^N) in the subject". That reasoning was wrong: the blow-up is exponential in the
#: PATTERN, not the subject. Measured on `(a+)+$` against `"a"*n + "X"`, `re` took 0.30s at n=22,
#: 4.86s at n=26 and 76.5s at n=30 — roughly 16x per four PATTERN-matched characters, at a subject
#: length of thirty. A 256 KiB cap is irrelevant to that. Kept because it is cheap and a real memory
#: bound; it is simply not the reason the stall cannot happen any more.
_REGEX_MAX_SUBJECT = 256 * 1024

#: Wall-clock budget for one regex arm. A BACKSTOP, not the primary defence: `regex` is not a naive
#: backtracker and answered the measurement above in 0.0003s at every n, so the engine swap is what
#: closes FLOWS-REDOS-ON-LOOP. This bounds whatever it does not optimise, turning a stall into an
#: ordinary node failure the builder can see instead of a wedged pod.
_REGEX_BUDGET_SECONDS = 2.0


def _regex(node: FlowNode, inputs: list[Payload]) -> Payload:
    """Extract matches or replace them. An unconfigured pattern PASSES THROUGH rather than failing —
    a node you have not filled in yet is not an error — and a bad pattern is a precise message."""
    payload = _one_input(node, inputs)
    pattern = node.config.get("regexPattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return payload
    if len(payload.text) > _REGEX_MAX_SUBJECT:
        raise NodeError(f"regex input too large: {len(payload.text)} chars (max {_REGEX_MAX_SUBJECT}) — split the flow upstream")
    try:
        compiled = regex.compile(pattern)
    except regex.error as exc:
        raise NodeError(f"bad pattern: {exc}") from exc
    # The APPLY is inside the boundary too, not just the compile. A pattern can compile and still
    # raise when used: `re.compile(r"(a)").sub(r"\\9", "aaa")` raises `re.error: invalid group
    # reference 9`, which is caller input and therefore a NodeError, not a crash. Outside the try it
    # escaped `run_node`'s `except NodeError`, propagated out of the activity, burned three
    # NODE_RETRY attempts on input no retry can fix, and failed the whole orchestration with no node
    # states — the precise outcome workflow.py says must not happen for a user-input refusal.
    try:
        if node.config.get("regexMode") == "replace":
            replacement = node.config.get("regexReplace")
            return Payload(text=compiled.sub(replacement if isinstance(replacement, str) else "", payload.text, timeout=_REGEX_BUDGET_SECONDS))
        # Capture group 1 when the pattern has one, else the whole match — matching the frontend.
        return Payload(text="\n".join(m.group(1) if m.lastindex else m.group(0) for m in compiled.finditer(payload.text, timeout=_REGEX_BUDGET_SECONDS)))
    except regex.error as exc:
        raise NodeError(f"bad pattern: {exc}") from exc
    except TimeoutError as exc:
        # The budget, not a crash: a pattern the engine cannot answer quickly is the caller's input,
        # so it fails this NODE and the rest of the graph carries on.
        raise NodeError(f"regex exceeded its {_REGEX_BUDGET_SECONDS}s budget — simplify the pattern") from exc


async def run_node(job: NodeJob, *, client: httpx.AsyncClient) -> NodeResult:
    """One node, timed, with failure captured as state rather than raised.

    `time.perf_counter` is the monotonic clock — a wall clock can step backwards under NTP and
    report a negative duration, which is exactly the kind of nonsense a timing chip must not show.
    """
    started = time.perf_counter()
    inputs = [Payload(text=text) for text in job.inputs]
    try:
        out = await dispatch(
            job.node,
            inputs,
            job.seed,
            client=client,
            serve_url=job.serve_url,
            serve_timeout=job.serve_timeout,
        )
    except NodeError as exc:
        return NodeResult(state=NodeRunState(status="failed", ms=_elapsed_ms(started), error=str(exc)))
    except Exception as exc:
        # A NODE FAILS AS STATE, NEVER AS A RAISE — that is this module's stated contract, and a
        # boundary that only catches NodeError enforces it only for the failures somebody remembered
        # to convert. Anything else escaped into the activity, where the durable lane retries it three
        # times against input no retry can fix and then fails the orchestration with no node states at
        # all: the builder sees a dead run and cannot tell which node killed it. The specific leak
        # found was `re.error` from `sub()`, now converted at source; this is the general guard so the
        # next one does not need finding first.
        log.exception("flows_node_crashed", extra={"node": job.node.id, "kind": job.node.kind})
        return NodeResult(state=NodeRunState(status="failed", ms=_elapsed_ms(started), error=f"{type(exc).__name__}: {exc}"))
    # BOTH fields are capped by the model, at different ceilings: `output_text` at the 4 000-char
    # DISPLAY cap, `payload_text` at `MAX_PAYLOAD_CHARS` (256 KiB) because it is the document the
    # graph carries on — and, in the durable lane, the value written to the workflow history once as
    # this activity's output and again as every dependent's input.
    return NodeResult(
        state=NodeRunState(status="succeeded", ms=_elapsed_ms(started), output_text=out.text),
        payload_text=out.text,
    )


async def execute(
    graph: FlowGraph,
    seeds: dict[str, str],
    run_id: str,
    *,
    client: httpx.AsyncClient,
    serve_url: str,
    serve_timeout: float = 180.0,
) -> RunState:
    """Run the whole graph inline, wave by wave, and return the finished RunState.

    A failed node BLOCKS its dependents: they are recorded `failed` with "upstream failed" and
    never dispatched. The alternative — running them against missing input — invents a second
    failure whose message is about the symptom, and the builder would paint the wrong node red.
    """
    waves = topo_waves(graph)
    if waves is None:
        return RunState(run_id=run_id, status="failed", error="graph has a cycle")

    by_id = {node.id: node for node in graph.nodes}
    incoming = upstreams(graph)
    states: dict[str, NodeRunState] = {}
    outputs: dict[str, str] = {}

    for wave in waves:
        runnable: list[str] = []
        for node_id in wave:
            blocked = [u for u in incoming[node_id] if states.get(u) is None or states[u].status == "failed"]
            if blocked:
                states[node_id] = NodeRunState(status="failed", error="upstream failed")
            else:
                runnable.append(node_id)

        jobs = [
            NodeJob(
                node=by_id[node_id],
                inputs=[outputs[u] for u in incoming[node_id]],
                seed=seeds.get(node_id),
                serve_url=serve_url,
                serve_timeout=serve_timeout,
            )
            for node_id in runnable
        ]
        # gather, not a loop: a wave is BY DEFINITION independent, and serializing it would make two
        # parallel model nodes cost the sum of their latencies — the thing waves exist to avoid.
        results = await asyncio.gather(*(run_node(job, client=client) for job in jobs))
        for node_id, result in zip(runnable, results, strict=True):
            states[node_id] = result.state
            if result.payload_text is not None:
                outputs[node_id] = result.payload_text

    failed = any(s.status == "failed" for s in states.values())
    return RunState(run_id=run_id, status="failed" if failed else "succeeded", nodes=states)


async def _call_serve(
    node: FlowNode,
    inputs: list[Payload],
    *,
    client: httpx.AsyncClient,
    serve_url: str,
    serve_timeout: float,
) -> Payload:
    app = node.config.get("app")
    if not isinstance(app, str) or not APP_PATTERN.match(app):
        raise NodeError('model node needs config["app"] matching ^[a-z0-9][a-z0-9_-]*$')

    payload = _one_input(node, inputs)
    url = f"{serve_url.rstrip('/')}/{app}"
    params = {}
    name = node.config.get("name")
    if isinstance(name, str) and name:
        # Serve's /htrflow ingress reads ?name= as the page key it stamps into the ALTO it returns.
        params["name"] = name

    content_type = "text/plain; charset=utf-8" if payload.kind == "text" else "application/octet-stream"
    try:
        resp = await client.post(
            url,
            content=payload.as_bytes(),
            params=params,
            headers={"content-type": content_type},
            # Explicit per-call, not inherited from the client: the same client serves the whole
            # fleet-shaped app, and one node's model budget is not the app's default budget.
            timeout=serve_timeout,
        )
    except httpx.RequestError as exc:
        # The exception CLASS, not its message: httpx interpolates the resolved address, which in
        # cluster is an internal one the caller cannot act on. The address they can act on is the
        # one they configured, so name that.
        raise NodeError(f"serve unreachable at {url} ({type(exc).__name__})") from exc

    if resp.status_code == 405:
        # The trap worth naming: `make serve-up` deploys `transcribe`, whose __call__ takes
        # list[Image] and is reachable only over a Serve DeploymentHandle — so a Serve that is
        # perfectly healthy answers 405 to every POST. The honest fix is `make serve-up-htrflow`.
        raise NodeError(f"serve app '{app}' has no HTTP ingress (405) — deploy an app that accepts POST")
    if resp.status_code >= 400:
        raise NodeError(f"serve returned {resp.status_code} for app '{app}'")

    return Payload(text=resp.text)


def _one_input(node: FlowNode, inputs: list[Payload]) -> Payload:
    if not inputs:
        raise NodeError(f"{node.kind} node has no upstream payload")
    # First edge wins for the single-input kinds, and it is the FIRST rather than a merge because
    # `alto`/`model` take one document; merging two would produce a body neither upstream authored.
    return inputs[0]


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
