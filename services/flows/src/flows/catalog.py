"""The server-declared node catalog — the five v0 kinds, mirroring open_studio_flows.md's table.

Why the server declares them at all when the frontend ships its own registry in v0: the catalog is
the SEAM. A node kind whose execution is server-side (a Ray stage, a governed-table read) cannot be
added by editing a Svelte file, and the palette has to learn about it from somewhere. Declaring the
five that already exist proves the seam end-to-end instead of building it later against nothing.

The response shape is pinned by `models.CatalogResponse`; this module owns only the CONTENT.
"""

from flows.models import NodeSpec, ParamSpec


#: The `name` query parameter Serve's `/htrflow` ingress accepts as the page key (it names the
#: page in the ALTO it returns). Shared by the two kinds that talk to Serve or read its output.
_PAGE_NAME = ParamSpec(name="name", label="Page name", type="string")

CATALOG: list[NodeSpec] = [
    NodeSpec(
        kind="image",
        label="Image",
        role="source",
        # No params: the payload IS the upload, and it never reaches this service (v0 keeps bytes on
        # the zone's own +server.ts). Declared anyway so the palette is complete and a server run
        # can refuse the kind by name rather than by "unknown".
        params=[_PAGE_NAME],
    ),
    NodeSpec(
        kind="text",
        label="Text",
        role="source",
        # `text` is the fallback; a run's `seeds[node_id]` wins, so the same graph can be replayed
        # with different inputs without editing the node.
        params=[ParamSpec(name="text", label="Text", type="string")],
    ),
    NodeSpec(
        kind="dataset",
        label="Lakehouse table",
        role="source",
        # SCAFFOLD, and it says so by REFUSING at run time (see executor.dispatch) rather than
        # emitting plausible rows. Reading a governed table means the Arrow lane, which is a
        # keep-bytes transport and therefore not this JSON service's job as it stands.
        params=[ParamSpec(name="table", label="Table", type="string")],
    ),
    NodeSpec(
        kind="prompt",
        label="Prompt",
        role="transform",
        # Pure string composition, so it runs identically here and in the browser — `{{input}}` is
        # substituted verbatim (prose, not a wire body; the JSON escaping belongs to `model`).
        params=[ParamSpec(name="promptTemplate", label="Template", type="string")],
    ),
    NodeSpec(
        kind="model",
        label="Model",
        role="transform",
        params=[
            # A select with NO options: the live Serve applications are the options, and only the
            # cluster knows them (`GET /api/serve/applications/`). See ParamSpec.options.
            ParamSpec(name="app", label="Serve app", type="select", required=True),
            _PAGE_NAME,
        ],
    ),
    NodeSpec(
        kind="mcp",
        label="MCP tool",
        role="transform",
        # SCAFFOLD, refused at run time like `dataset`: MCP servers here are Claude Code's session
        # tooling, not a service this fleet can reach — calling one needs an MCP client and a
        # stdio/SSE transport that does not exist.
        params=[
            ParamSpec(name="mcpServer", label="Server", type="string"),
            ParamSpec(name="mcpTool", label="Tool", type="string"),
            ParamSpec(name="mcpArgs", label="Arguments (JSON)", type="string"),
        ],
    ),
    NodeSpec(
        kind="alto",
        label="ALTO → text",
        role="transform",
        params=[],
    ),
    NodeSpec(
        kind="extract",
        label="Extract JSON",
        role="transform",
        params=[ParamSpec(name="extractPath", label="Path", type="string")],
    ),
    NodeSpec(
        kind="regex",
        label="Regex",
        role="transform",
        params=[
            ParamSpec(name="regexMode", label="Mode", type="select", options=["extract", "replace"]),
            ParamSpec(name="regexPattern", label="Pattern", type="string"),
            ParamSpec(name="regexReplace", label="Replacement", type="string"),
        ],
    ),
    NodeSpec(
        kind="compare",
        label="Compare",
        role="sink",
        # The one MULTI-input kind: it reads every upstream payload, not the first.
        params=[],
    ),
    NodeSpec(
        kind="inspect",
        label="Inspect",
        role="sink",
        params=[],
    ),
]

#: The kinds `graph.validate_graph` will accept. Derived, never a second hand-kept list.
KNOWN_KINDS: frozenset[str] = frozenset(spec.kind for spec in CATALOG)
