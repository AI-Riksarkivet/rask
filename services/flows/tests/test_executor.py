"""Node dispatch and the inline executor. The Serve boundary is mocked at the TRANSPORT (respx),
so every assertion is about the request that would actually reach the cluster."""

import httpx
import pytest
import respx

from flows.executor import _MISSING, _REGEX_MAX_SUBJECT, NodeError, alto_lines, compare_texts, dispatch, execute, pick_path, run_node
from flows.models import MAX_PAYLOAD_CHARS, FlowEdge, FlowGraph, FlowNode, NodeJob, NodeResult, NodeRunState, Payload


SERVE = "http://serve.test:8000"

ALTO = """<?xml version="1.0"?>
<alto xmlns="http://www.loveholidays.com/alto">
  <TextLine ID="l1">
    <String CONTENT="Anno" WC="0.9"/>
    <String CONTENT="1723" WC="0.8"/>
  </TextLine>
  <TextLine ID="l2">
    <String CONTENT="Bengt &amp; Anna" WC="0.7"/>
  </TextLine>
</alto>"""


@pytest.fixture
def client():
    return httpx.AsyncClient()


# ---- alto extraction -------------------------------------------------------------------------


def test_alto_lines_group_strings_per_textline() -> None:
    assert alto_lines(ALTO) == ["Anno 1723", "Bengt & Anna"]


def test_alto_lines_unescape_the_entities_alto_actually_carries() -> None:
    xml = '<TextLine><String CONTENT="&quot;kvar&quot; &lt;7&gt; &amp; O&apos;Neil"/></TextLine>'
    assert alto_lines(xml) == ['"kvar" <7> & O\'Neil']


def test_alto_lines_skip_empty_textlines_rather_than_emitting_blanks() -> None:
    xml = '<TextLine ID="a"><String CONTENT="one"/></TextLine><TextLine ID="b"/>'
    assert alto_lines(xml) == ["one"]


def test_alto_lines_fall_back_to_bare_strings_only_when_there_is_no_textline() -> None:
    """A Serve app that returns Strings without TextLines still yields something. The fallback is
    conditional on there being NO TextLine at all — a page whose TextLines are genuinely empty must
    stay empty rather than collapse into one line of every word on the page."""
    assert alto_lines('<String CONTENT="loose"/><String CONTENT="words"/>') == ["loose words"]
    assert alto_lines("<alto></alto>") == []


def test_alto_lines_are_not_confused_by_a_textstyle_element() -> None:
    # `<TextLine` must match on a word boundary; a naive prefix match would swallow siblings.
    xml = '<TextStyle ID="s"/><TextLine><String CONTENT="real"/></TextLine>'
    assert alto_lines(xml) == ["real"]


# ---- per-node dispatch -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_node_prefers_the_run_seed_over_the_stored_config(client: httpx.AsyncClient) -> None:
    node = FlowNode(id="t", kind="text", config={"text": "saved"})
    async with client:
        seeded = await dispatch(node, [], "fresh", client=client, serve_url=SERVE)
        stored = await dispatch(node, [], None, client=client, serve_url=SERVE)
    assert seeded.text == "fresh"
    assert stored.text == "saved"


@pytest.mark.asyncio
async def test_image_node_is_refused_server_side(client: httpx.AsyncClient) -> None:
    """Bytes are a frontend-only lane in v0 (the transport ruling). A server run that reaches an
    image node must say so, not invent an empty payload."""
    async with client:
        with pytest.raises(NodeError, match="image seeds are frontend-only in v0"):
            await dispatch(FlowNode(id="i", kind="image"), [], None, client=client, serve_url=SERVE)


@pytest.mark.asyncio
@respx.mock
async def test_model_node_posts_the_upstream_payload_to_the_serve_app(client: httpx.AsyncClient) -> None:
    route = respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text="<alto/>"))
    node = FlowNode(id="m", kind="model", config={"app": "htrflow", "name": "page-7"})

    async with client:
        out = await dispatch(node, [Payload(text="body")], None, client=client, serve_url=SERVE)

    assert out.text == "<alto/>"
    assert route.called
    request = route.calls.last.request
    assert request.content == b"body"
    assert request.url.params["name"] == "page-7"


@pytest.mark.asyncio
async def test_model_node_refuses_an_app_name_that_could_escape_the_url(client: httpx.AsyncClient) -> None:
    """The app name comes from a drawn graph and is concatenated into a URL. `../` or a leading slash
    would aim the POST at an arbitrary path on the Serve host."""
    node = FlowNode(id="m", kind="model", config={"app": "../admin"})
    async with client:
        with pytest.raises(NodeError, match=r"\^\[a-z0-9\]"):
            await dispatch(node, [Payload(text="x")], None, client=client, serve_url=SERVE)


@pytest.mark.asyncio
async def test_model_node_without_an_upstream_says_so(client: httpx.AsyncClient) -> None:
    node = FlowNode(id="m", kind="model", config={"app": "htrflow"})
    async with client:
        with pytest.raises(NodeError, match="no upstream payload"):
            await dispatch(node, [], None, client=client, serve_url=SERVE)


@pytest.mark.asyncio
@respx.mock
async def test_a_405_from_serve_names_the_real_cause(client: httpx.AsyncClient) -> None:
    """`make serve-up` deploys `transcribe`, whose __call__ takes list[Image] and is reachable only
    over a DeploymentHandle — so a perfectly healthy Serve answers 405 to every POST. "405" alone
    sends the operator looking at the wrong thing."""
    respx.post(f"{SERVE}/transcribe").mock(return_value=httpx.Response(405))
    node = FlowNode(id="m", kind="model", config={"app": "transcribe"})
    async with client:
        with pytest.raises(NodeError, match="no HTTP ingress"):
            await dispatch(node, [Payload(text="x")], None, client=client, serve_url=SERVE)


@pytest.mark.asyncio
@respx.mock
async def test_an_unreachable_serve_names_the_configured_address_not_the_resolved_one(client: httpx.AsyncClient) -> None:
    respx.post(f"{SERVE}/htrflow").mock(side_effect=httpx.ConnectError("nope"))
    node = FlowNode(id="m", kind="model", config={"app": "htrflow"})
    async with client:
        with pytest.raises(NodeError, match=r"serve unreachable at http://serve\.test:8000/htrflow \(ConnectError\)"):
            await dispatch(node, [Payload(text="x")], None, client=client, serve_url=SERVE)


@pytest.mark.asyncio
async def test_inspect_node_concatenates_its_upstreams_in_edge_order(client: httpx.AsyncClient) -> None:
    async with client:
        out = await dispatch(
            FlowNode(id="s", kind="inspect"),
            [Payload(text="first"), Payload(text="second")],
            None,
            client=client,
            serve_url=SERVE,
        )
    assert out.text == "first\nsecond"


# ---- whole-graph execution -------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_execute_runs_a_text_model_alto_chain() -> None:
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=ALTO))
    graph = FlowGraph(
        nodes=[
            FlowNode(id="t", kind="text"),
            FlowNode(id="m", kind="model", config={"app": "htrflow"}),
            FlowNode(id="a", kind="alto"),
        ],
        edges=[FlowEdge(source="t", target="m"), FlowEdge(source="m", target="a")],
    )

    async with httpx.AsyncClient() as client:
        state = await execute(graph, {"t": "seed"}, "run-1", client=client, serve_url=SERVE)

    assert state.status == "succeeded"
    assert state.nodes["a"].output_text == "Anno 1723\nBengt & Anna"
    assert all(n.ms is not None for n in state.nodes.values())


@pytest.mark.asyncio
@respx.mock
async def test_a_failed_node_blocks_its_dependents_without_dispatching_them() -> None:
    """The dependent is recorded `failed` / "upstream failed" and never run. Running it against
    missing input would invent a second failure and the builder would paint the wrong node red."""
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(500))
    graph = FlowGraph(
        nodes=[
            FlowNode(id="t", kind="text"),
            FlowNode(id="m", kind="model", config={"app": "htrflow"}),
            FlowNode(id="a", kind="alto"),
            FlowNode(id="s", kind="inspect"),
        ],
        edges=[
            FlowEdge(source="t", target="m"),
            FlowEdge(source="m", target="a"),
            FlowEdge(source="a", target="s"),
        ],
    )

    async with httpx.AsyncClient() as client:
        state = await execute(graph, {"t": "seed"}, "run-2", client=client, serve_url=SERVE)

    assert state.status == "failed"
    assert state.nodes["t"].status == "succeeded"
    assert state.nodes["m"].error == "serve returned 500 for app 'htrflow'"
    assert state.nodes["a"].error == "upstream failed"
    assert state.nodes["s"].error == "upstream failed"
    # Blocked nodes were never dispatched, so they have no duration to report.
    assert state.nodes["a"].ms is None


@pytest.mark.asyncio
async def test_execute_refuses_a_cycle_without_running_anything() -> None:
    graph = FlowGraph(
        nodes=[FlowNode(id="a", kind="text"), FlowNode(id="b", kind="inspect")],
        edges=[FlowEdge(source="a", target="b"), FlowEdge(source="b", target="a")],
    )
    async with httpx.AsyncClient() as client:
        state = await execute(graph, {}, "run-3", client=client, serve_url=SERVE)
    assert state.status == "failed"
    assert state.error == "graph has a cycle"
    assert state.nodes == {}


@pytest.mark.asyncio
@respx.mock
async def test_a_downstream_node_receives_the_full_payload_not_the_display_truncation() -> None:
    """`NodeRunState.output_text` is capped for display at 4 000 characters; the payload the graph
    carries is not bounded by THAT — its own ceiling is two orders of magnitude higher (see
    `test_a_payload_past_its_own_ceiling_is_cut_loudly`). Feeding the display copy downstream would
    hand an alto node a document cut mid-element."""
    long_alto = "<TextLine>" + "".join(f'<String CONTENT="w{i}"/>' for i in range(1200)) + "</TextLine>"
    assert len(long_alto) > 4000
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=long_alto))
    graph = FlowGraph(
        nodes=[
            FlowNode(id="t", kind="text"),
            FlowNode(id="m", kind="model", config={"app": "htrflow"}),
            FlowNode(id="a", kind="alto"),
        ],
        edges=[FlowEdge(source="t", target="m"), FlowEdge(source="m", target="a")],
    )

    async with httpx.AsyncClient() as client:
        state = await execute(graph, {"t": "x"}, "run-4", client=client, serve_url=SERVE)

    assert state.status == "succeeded"
    assert len(state.nodes["m"].output_text or "") == 4000  # display cap held on the model node

    # 4000 characters of that XML covers roughly the first 170 `<String>` elements, so a `w500` in the
    # alto node's text is only possible if it was fed the UNCAPPED payload. Asserting the last word
    # instead would be asserting the wrong thing: the alto node's own output_text is capped too (it
    # ends at w821), which is display truncation working, not the payload being short.
    words = (state.nodes["a"].output_text or "").split()
    assert "w500" in words
    assert len(state.nodes["a"].output_text or "") == 4000


@pytest.mark.asyncio
@respx.mock
async def test_a_payload_past_its_own_ceiling_is_cut_loudly() -> None:
    """The value `run_node` returns is the DURABLE one: it lands in the workflow history as this
    activity's output and again as the input of every dependent node, so an unbounded payload costs
    O(dependents) writes to the state store.

    Two properties, and the second is what makes the first acceptable: the payload stops at
    `MAX_PAYLOAD_CHARS` (so it also still fits `_REGEX_MAX_SUBJECT`, the other bound a payload meets
    one edge downstream), and it SAYS it was cut. A silent truncation here would hand an alto node a
    document ending mid-element and a plausible short answer; a marked one cannot be mistaken for
    the whole document.
    """
    oversize = "x" * (MAX_PAYLOAD_CHARS + 5_000)
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=oversize))
    job = NodeJob(node=FlowNode(id="m", kind="model", config={"app": "htrflow"}), inputs=["seed"], serve_url=SERVE)

    async with httpx.AsyncClient() as client:
        result = await run_node(job, client=client)

    assert result.state.status == "succeeded"
    payload = result.payload_text or ""
    assert len(payload) == MAX_PAYLOAD_CHARS
    assert len(payload) <= _REGEX_MAX_SUBJECT
    assert str(MAX_PAYLOAD_CHARS + 5_000) in payload  # the marker names the ORIGINAL length
    assert payload.endswith("-character ceiling]")


@pytest.mark.asyncio
@respx.mock
async def test_a_payload_under_the_ceiling_is_untouched() -> None:
    """The cap is a ceiling, not a transform: every real payload passes through byte-for-byte, so a
    marker in a downstream input always means something."""
    body = '<TextLine><String CONTENT="Anno"/></TextLine>'
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=body))
    job = NodeJob(node=FlowNode(id="m", kind="model", config={"app": "htrflow"}), inputs=["seed"], serve_url=SERVE)

    async with httpx.AsyncClient() as client:
        result = await run_node(job, client=client)

    assert result.payload_text == body


def test_the_cap_survives_a_workflow_replay_unchanged() -> None:
    """REPLAY HYGIENE, which is what makes a truncating validator safe to put on a durable contract.

    `workflow.flow_run_workflow` re-validates every activity result out of the history
    (`NodeResult.model_validate(raw)`) on every replay, and hands `payload_text` on as the next
    node's input — where it is validated again. A cap that cut a second time would make one history
    yield a different payload on each replay (and compound the marker), which is the non-determinism
    the workflow's own docstring bans. Idempotent, so it does not.
    """
    once = NodeResult(state=NodeRunState(status="succeeded"), payload_text="y" * (MAX_PAYLOAD_CHARS * 2))
    twice = NodeResult.model_validate(once.model_dump())
    thrice = NodeResult.model_validate(twice.model_dump())

    assert len(once.payload_text or "") == MAX_PAYLOAD_CHARS
    assert twice.payload_text == once.payload_text
    assert thrice.payload_text == once.payload_text


# ── the two kinds added with the frontend's palette (prompt, dataset) ─────────────────────────────


@pytest.mark.asyncio
async def test_prompt_composes_the_template_with_the_upstream_text() -> None:
    node = FlowNode(id="p", kind="prompt", config={"promptTemplate": "Summarise: {{input}}"})
    async with httpx.AsyncClient() as client:
        out = await dispatch(node, [Payload(text="anno 1723")], None, client=client, serve_url="http://x")
    assert out.text == "Summarise: anno 1723"


@pytest.mark.asyncio
async def test_prompt_without_a_template_refuses() -> None:
    node = FlowNode(id="p", kind="prompt", config={})
    async with httpx.AsyncClient() as client:
        with pytest.raises(NodeError, match="no template"):
            await dispatch(node, [Payload(text="x")], None, client=client, serve_url="http://x")


@pytest.mark.asyncio
async def test_dataset_refuses_rather_than_emitting_fake_rows() -> None:
    """The scaffold must FAIL, not fabricate: a flow built on it must not read as working."""
    node = FlowNode(id="d", kind="dataset", config={"table": "wh.ns.pages"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(NodeError, match="scaffold"):
            await dispatch(node, [], None, client=client, serve_url="http://x")


# ── extract / regex / compare / mcp (the nodes added with the palette's second wave) ──────────────


def test_pick_path_walks_bracket_and_dotted_indices() -> None:
    doc = {"choices": [{"message": {"content": "hej"}}], "data": [{"embedding": [0.1, 0.2]}]}
    assert pick_path(doc, "choices[0].message.content") == "hej"
    assert pick_path(doc, "choices.0.message.content") == "hej"
    assert pick_path(doc, "data[0].embedding[1]") == 0.2


def test_pick_path_reports_a_miss_instead_of_raising() -> None:
    doc = {"a": {"b": 1}}
    assert pick_path(doc, "a.nope") is _MISSING
    assert pick_path(doc, "a.b.c") is _MISSING  # scalar with path left to walk
    assert pick_path(doc, "a[0]") is _MISSING  # index into a mapping
    # A genuine null is a VALUE, not a miss — that is why the sentinel exists.
    assert pick_path({"a": None}, "a") is None


@pytest.mark.asyncio
async def test_extract_pulls_the_field_out_of_a_model_envelope() -> None:
    node = FlowNode(id="x", kind="extract", config={"extractPath": "choices[0].message.content"})
    envelope = Payload(text='{"choices":[{"message":{"content":"anno 1723"}}]}')
    async with httpx.AsyncClient() as client:
        out = await dispatch(node, [envelope], None, client=client, serve_url=SERVE)
    # Unquoted: the whole point is that downstream sees the ANSWER, not a JSON string literal.
    assert out.text == "anno 1723"


@pytest.mark.asyncio
async def test_extract_refuses_non_json_and_a_bad_path() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NodeError, match="not JSON"):
            await dispatch(
                FlowNode(id="x", kind="extract", config={"extractPath": "a"}),
                [Payload(text="<alto/>")],
                None,
                client=client,
                serve_url=SERVE,
            )
        with pytest.raises(NodeError, match="no value at"):
            await dispatch(
                FlowNode(id="x", kind="extract", config={"extractPath": "nope"}),
                [Payload(text='{"a":1}')],
                None,
                client=client,
                serve_url=SERVE,
            )


@pytest.mark.asyncio
async def test_regex_extracts_capture_groups_and_replaces() -> None:
    async with httpx.AsyncClient() as client:
        got = await dispatch(
            FlowNode(id="r", kind="regex", config={"regexPattern": r"Anno (\d{4})"}),
            [Payload(text="Anno 1723 and Anno 1724")],
            None,
            client=client,
            serve_url=SERVE,
        )
        assert got.text == "1723\n1724"  # group 1, every match

        replaced = await dispatch(
            FlowNode(
                id="r",
                kind="regex",
                config={"regexMode": "replace", "regexPattern": r"\d+", "regexReplace": "N"},
            ),
            [Payload(text="1723 and 1724")],
            None,
            client=client,
            serve_url=SERVE,
        )
        assert replaced.text == "N and N"


@pytest.mark.asyncio
async def test_regex_passes_through_when_unconfigured_and_refuses_a_bad_pattern() -> None:
    async with httpx.AsyncClient() as client:
        passed = await dispatch(
            FlowNode(id="r", kind="regex", config={}),
            [Payload(text="untouched")],
            None,
            client=client,
            serve_url=SERVE,
        )
        assert passed.text == "untouched"
        with pytest.raises(NodeError, match="bad pattern"):
            await dispatch(
                FlowNode(id="r", kind="regex", config={"regexPattern": "([unclosed"}),
                [Payload(text="x")],
                None,
                client=client,
                serve_url=SERVE,
            )


def test_compare_texts_reports_both_sides_and_the_first_difference() -> None:
    report = compare_texts(["line one\nline two", "line one\nline TWO"])
    assert "2 inputs compared" in report
    assert "── verdict: A and B differ ──" in report
    assert "first difference at line 2" in report
    assert compare_texts(["same", "same"]).endswith("── verdict: A and B are identical ──")


@pytest.mark.asyncio
async def test_compare_reads_EVERY_input_not_the_first() -> None:
    node = FlowNode(id="c", kind="compare", config={})
    async with httpx.AsyncClient() as client:
        out = await dispatch(node, [Payload(text="gemma says A"), Payload(text="qwen says B")], None, client=client, serve_url=SERVE)
    assert "gemma says A" in out.text and "qwen says B" in out.text
    assert "2 inputs compared" in out.text


@pytest.mark.asyncio
async def test_mcp_refuses_and_names_the_missing_piece() -> None:
    node = FlowNode(id="m", kind="mcp", config={"mcpServer": "ra-mcp", "mcpTool": "search_transcribed"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(NodeError, match="scaffold"):
            await dispatch(node, [], None, client=client, serve_url=SERVE)


def test_regex_refuses_an_oversized_subject_by_name() -> None:
    """FLOWS-REDOS-ON-LOOP: the CPU arms now run off the loop, but a thread cannot stop a GIL stall,
    so the subject-length cap is the load-bearing half — an unbounded caller-influenced subject under
    a backtracking pattern froze the pod, probes included."""
    from flows.executor import _REGEX_MAX_SUBJECT, NodeError, _regex
    from flows.models import FlowNode, Payload

    node = FlowNode(id="r1", kind="regex", config={"regexPattern": "(a+)+$"})
    big = Payload(text="a" * (_REGEX_MAX_SUBJECT + 1))
    with pytest.raises(NodeError, match="too large"):
        _regex(node, [big])
    small = _regex(node, [Payload(text="aaa")])
    assert small.text  # a bounded subject still runs
