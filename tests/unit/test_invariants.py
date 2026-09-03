"""CLAIM-LINT — the mechanical guards for the bug class that kept escaping this repo (GOAL-prove-it P0.2).

Three real, shipped bugs motivated each guard below. Every one passed the entire unit + integration suite
and a manual "live-verified" run, because a prose CLAIM was never mechanically checked:

  1. "#4: every lineage publish is staged through the outbox" — THREE publishers bypassed it (the media
     head + two FAIL emits, one on a _DROP path where a lost publish erased the failure forever). I had
     verified the ONE publisher I changed and never grepped for the rest.
  2. "MEDALLION_LINEAGE_OUTBOX_URI is wired" — the chart injected it and no code ever read it (a dead env).
     A whole feature was configured and inert.
  3. "seed_warehouse grants the FGA parent edge" — it wrote `warehouse#parent`, a relation the warehouse
     type does NOT define (its pointer is `project`). OpenFGA rejected the write → a live 503, while every
     unit test stayed green because mocked `fga.check`/`write_tuples` pin the STRING, never the SCHEMA.

The rule these encode: a claim that cannot be proven by a grep, a test, or a render is not a claim — it is
a guess. Each test below fails on the ORIGINAL buggy code and passes now.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, NamedTuple
from urllib.parse import urlparse

import pytest
import yaml


REPO = Path(__file__).resolve().parents[2]
SERVICES = REPO / "services"
# The rask merge converted every service to src-layout (services/<n>/src/<n>/…) and merged the
# shared lib into service-kit (packages/service-kit/src/service_kit). rglob("*.py") over SERVICES
# still sees every service module; these two helpers resolve the paths the assertions name.
SERVICE_KIT = REPO / "packages" / "service-kit" / "src" / "service_kit"


def _svc(name: str) -> pathlib.Path:
    """Source root of a copied lance-ns service under the src-layout."""
    return SERVICES / name / "src" / name


CHART = REPO / "chart"
MAKEFILE = REPO / "Makefile"


# --------------------------------------------------------------------------------------------------
# 1. #4 outbox uniformity — zero bare publishes to the LINEAGE topic
# --------------------------------------------------------------------------------------------------


#: Every publish site in `services/`, classified by WHAT THE TOPIC CARRIES.
#:
#: A declare-your-intent registry, not a pattern match, and the reason is that the pattern match did not
#: work. The guard this replaces looked for the literal `topic_name=settings.lineage_topic` inside a
#: `dapr_publish.publish_event(` window — and EVERY site in the estate names its topic through a variable
#: (`self._topic`, `settings.pub_topic`, ...), so it matched nothing. It was a test that could only pass,
#: green for as long as it existed while two bare lineage publishes sat in front of it.
#:
#: A better regex cannot fix that: both lineage offenders take the topic as a CONSTRUCTOR argument, so no
#: pattern over the call site can resolve what it publishes to. The only thing that can is a human saying
#: so once, here — and a new or moved site failing until somebody does.
#:
#: Keyed by (module, topic expression): both survive edits that a line number would not.
_PUBLISH_INTENT: Final[dict[tuple[str, str], str]] = {
    # LINEAGE — an event DESCRIBING a committed write. Losing one means the data landed and the graph
    # never learned of it, so these must be staged through `outbox.publish_lineage_with_outbox`.
    # LINEAGE-RELAY — the outbox's OWN redelivery, and deliberately not "lineage-bare". The event it
    # publishes was already staged (that is where the drain read it from), so it is the durable path's
    # delivery half rather than a producer skipping the outbox. Publishing the STAGED BYTES verbatim,
    # before the staged object is dropped, is what makes a recovered event restart a halted cascade
    # instead of merely repairing the graph.
    ("services/lineage/src/lineage/api/reconcile_cron.py", "settings.dapr_topic"): "lineage-relay",
    # CONTROL — no longer a row here, and the reason is a change to the estate rather than to this file.
    # `DaprControlEmitter.emit` used to publish DIRECTLY; it now goes through
    # `outbox.publish_with_outbox`, which is a `_TRANSPORT_MODULE` and so is deliberately unclassified
    # (a transport publishes whatever its caller chose). The control lane's durability question moved
    # with it: a dropped `table_published` is no longer merely "a refresh hint" — under the tag-driven
    # cascade it is the ONLY thing that wakes the next hop — and staging is what answers that now.
    # CONTROL-RELAY — the control outbox's OWN redelivery, and the twin of `lineage-relay` above rather
    # than a plain "control" producer. The event it publishes was already staged (that is where the drain
    # read it from), so this is the durable path's delivery half, not a producer skipping the outbox.
    # It publishes the STAGED BYTES verbatim, before dropping the object, because `event_id` is what the
    # cascade's deterministic workflow instance id is derived from — re-minting the event would drive the
    # hop twice instead of re-attaching to it.
    ("services/catalog/src/catalog/api/control_relay.py", "CONTROL_TOPIC"): "control-relay",
    # TRIGGER — an instruction to DO work. Correctly bare: the outbox re-ingests lineage, it never
    # re-fires triggers. Their durability question is a different one, and is DECIDED: the caller-retry
    # idempotency-token contract is the carrier (docs/architecture/medallion-cascade.md, the dropped obligation-carrier ruling).
    ("services/medallion/src/medallion/services/ingest_trigger.py", "settings.bronze_topic"): "trigger",
    ("services/medallion/src/medallion/services/media_produce.py", "settings.media_topic"): "trigger",
    # The variable is now `topic` — resolved per publication from `settings.lane_routes` rather than
    # fixed to bronze, which is what let a silver publication fire a bronze trigger.
    ("services/medallion/src/medallion/services/publication_trigger.py", "topic"): "trigger",
    ("services/medallion/src/medallion/services/train.py", "settings.train_topic"): "trigger",
    # The on-demand compaction door. A TRIGGER, not lineage: it publishes an instruction to maintain one
    # dataset, and the lineage for that maintenance is emitted by the EXECUTOR once the work is actually
    # done (`work.py::handle_unit` -> `emit_sweep_lineage`). Staging it through the lineage outbox would
    # re-ingest an instruction as though it described a committed write. Its durability question is the
    # trigger one and has the trigger answer: the caller holds the 202 and can re-click, and the hourly
    # cron backstop re-plans the dataset regardless.
    ("services/catalog/src/catalog/api/v1/endpoints/maintenance.py", "settings.maintenance_work_topic"): "trigger",
    # A maintenance work UNIT: "compact and GC this one dataset". An instruction, so correctly bare —
    # and its durability question is answered by the plan rather than by the outbox. A unit that is lost
    # is re-planned by the next cron tick from the CURRENT manifest, which is strictly better than
    # replaying a stale one: a re-delivered old unit names fragments a later compaction may already have
    # rewritten, and carries a protection verdict computed against an estate that no longer exists.
    # Staging these would make maintenance durable in exactly the way it must not be.
    ("services/maintenance/src/maintenance/services/work_queue.py", "topic"): "trigger",
    # `transform.py` (settings.pub_topic) IS DELIBERATELY ABSENT. The mover fired the next stage's
    # topic itself — a SECOND enforcement point beside the catalog's tag move, and the DEFAULT one
    # because MEDALLION_CASCADE_VIA_PUBLISH shipped False. Deleted with `GateOutcome.TRIGGER`; the
    # cascade advances only through `publication_trigger.py` above. This registry's stale-entry check
    # is what forced the row to be removed rather than left behind describing a door that is gone.
    ("services/medallion/src/medallion/workflow.py", "settings.sub_topic"): "trigger",
    # The withheld next-stage trigger, released by an approval. A TRIGGER like every other cascade
    # publish: losing one stalls the cascade, it does not lose a committed fact.
    ("services/medallion/src/medallion/workflow.py", "spec.pub_topic"): "trigger",
    # The ASK: a held promotion telling its approver there is something to decide. CONTROL, not
    # lineage — the hold's own lineage FAIL already records what happened to the data; this records
    # what is being asked of a person, and a lost one costs a re-read rather than a committed fact.
    ("services/medallion/src/medallion/workflow.py", "CONTROL_TOPIC"): "control",
    # The HOLD, handed from the mover that made it to the app that can host the review. A TRIGGER:
    # it instructs the producer to start a `promotion_review` instance, and losing one leaves the
    # promotion blocked — the same stalled-cascade failure every other trigger has, and the same
    # answer (the transform's caller-retry token). Distinct from the CONTROL row above, which is the
    # notification the started review then sends to a PERSON.
    ("services/medallion/src/medallion/services/promotion_hold.py", "settings.promotion_topic"): "trigger",
}

#: The lineage publishes that do NOT go through the outbox. DEFERRED WITH A STATED TRADE, not an oversight.
#:
#: Routing `catalog` through the outbox is a LATENCY decision on a user-facing path, not a mechanical
#: change. `catalog/core/lineage_emit.py` awaits its emit INLINE on purpose — "so the event reaches the
#: durable Dapr/JetStream transport BEFORE the response returns; `BackgroundTasks` have no retry and die
#: with the worker" — so the estate already chose durability-before-response over a fast response once,
#: and rejected the obvious way to move the work off the request path.
#:
#: The outbox would serve that same intent BETTER (a staged object survives a failed publish, where today
#: the failure is swallowed and the event is simply lost) and would cost an S3 write on every table
#: create/write. Neither service has `outbox_uri` config, so it also needs new settings and chart wiring
#: in both. What is missing is not the code but a latency budget for the catalog write path to judge the
#: trade against; nothing in the estate measures one today.
#:
#: `maintenance` is de-prioritised on its own terms: a compaction mints no logical data, its event is
#: deliberately versionless, and reconcile's `latest_write_version` excludes it — so there is no
#: data-vs-graph divergence for the outbox to prevent.
#:
#: The prerequisite that made widening DANGEROUS is gone: the staged object is keyed per EVENT now, so
#: adding producers can no longer spread a key collision (`outbox._object_key`).
#:
#: This set may shrink. It may not grow.
_KNOWN_BARE_LINEAGE: Final[frozenset[str]] = frozenset(module for (module, _topic), intent in _PUBLISH_INTENT.items() if intent == "lineage-bare")

#: The PIPE, not a caller. Both forward whatever topic they are handed (`**kwargs`; a `topic_name`
#: parameter), so neither carries an intent to declare — "lineage" or "control" is a property of the
#: caller that chose the topic, and stamping one here would assert a classification for every OTHER
#: caller that happens to flow through. Excluded so the registry keeps meaning "who publishes what",
#: which is the question it exists to answer.
_TRANSPORT_MODULES: Final[frozenset[str]] = frozenset(
    {
        "packages/service-kit/src/service_kit/dapr_publish.py",
        "packages/service-kit/src/service_kit/lakehouse/outbox.py",
    }
)

#: Both spellings of a publish. `publish_json` is `dapr_publish`'s serialize-and-report wrapper over
#: `publish_event` (open_python-audit DUP-18): the five medallion trigger sites call it now, and a
#: pattern that knew only `publish_event` stopped seeing every one of them — the registry going quiet
#: about five real publishers, which is this guard's own failure mode arriving through the scan.
_PUBLISH_CALL = re.compile(r"\bpublish_(event|json)\(")
_TOPIC_KWARG = re.compile(r"topic_name=([A-Za-z_][A-Za-z_0-9\.\[\]]*)")


def _observed_publish_sites() -> dict[tuple[str, str], str]:
    """Every publish site under `services/`, as (module, topic expression) -> "file:line".

    Matches a BARE `publish_event(` rather than the dotted spelling, because both are in use: most sites
    call `dapr_publish.publish_event(...)`, while `medallion/workflow.py` imports the wrapper directly
    (`from service_kit.dapr_publish import publish_event`) and calls it unqualified. A guard keyed on the
    dotted form misses that one — which is exactly the shape of the bug this registry replaces.
    """
    observed: dict[tuple[str, str], str] = {}
    # `packages/` is walked too, and not as a completeness flourish: the control emitter MOVED there when
    # its third producer appeared, and a scanner rooted only at `services/` would have stopped seeing a
    # real publish site the moment it became shared. That is precisely this guard's own failure mode —
    # a publisher nobody classified — reintroduced by the scan's shape rather than by a new caller.
    roots = (SERVICES, REPO / "packages")
    for py in sorted(path for root in roots for path in root.rglob("*.py")):
        if "tests" in py.parts or py.relative_to(REPO).as_posix() in _TRANSPORT_MODULES:
            continue
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            if not _PUBLISH_CALL.search(line):
                continue
            topic = _TOPIC_KWARG.search("\n".join(lines[i : i + 10]))
            module = str(py.relative_to(REPO))
            observed[(module, topic.group(1) if topic else "<no topic_name>")] = f"{module}:{i + 1}"
    return observed


def test_every_publish_site_declares_what_its_topic_carries() -> None:
    """A new publish site must be classified before it can ship.

    The point is the CLASSIFICATION, not the count: whether an event describes a write, hints at a refresh,
    or commands work decides whether losing it is recoverable — and that is a judgement only the author can
    make. Failing here is the prompt to make it.
    """
    observed = _observed_publish_sites()
    undeclared = sorted(location for key, location in observed.items() if key not in _PUBLISH_INTENT)
    assert not undeclared, (
        f"these publish sites are not classified in _PUBLISH_INTENT: {undeclared}. Add each one as "
        "'lineage' (describes a committed write -> must be staged through the outbox), 'control' (a "
        "best-effort refresh hint) or 'trigger' (an instruction to do work). The outbox's scope, and why "
        "Dapr's own cannot replace it, are in docs/DECISIONS.md."
    )
    stale = sorted(f"{module} ({topic})" for (module, topic) in _PUBLISH_INTENT if (module, topic) not in observed)
    assert not stale, (
        f"_PUBLISH_INTENT declares publish sites that no longer exist: {stale}. A stale entry makes the guard describe an estate that is not there — delete it."
    )


def test_the_set_of_bare_lineage_publishes_does_not_grow() -> None:
    """#4 claims 'every lineage publish is staged'. As of 2026-08-25 that is mechanically TRUE: zero.

    The set was two — the catalog's and maintenance's emitters — and this pinned the number so the debt
    was VISIBLE rather than silently missed, as it was by the guard this replaces. Both are now routed
    through `outbox.publish_lineage_with_outbox`, so `_KNOWN_BARE_LINEAGE` is empty and the assertion
    below now says something stronger than it used to: not "the debt has not grown" but "there is none".

    It stays as a ratchet rather than being deleted, because an empty set is exactly what a new bare
    publisher would grow. The fix had a prerequisite that is worth remembering if this ever regresses:
    `stage_event` keys the staged object on `<run_id>.json` while the run id excludes event_type, so a
    COMPLETE and a FAIL for one run once shared one object — `transform.py` documents that having
    destroyed a staged COMPLETE. Routing producers through the outbox before that key was fixed would
    have spread a lossy implementation. The key is fixed; what remains is the latency trade.
    """
    observed = _observed_publish_sites()
    bare = {module for (module, _topic), location in observed.items() if module in _KNOWN_BARE_LINEAGE}
    assert bare == set(_KNOWN_BARE_LINEAGE), (
        f"the bare-lineage-publish set changed: {sorted(bare)} vs the pinned {sorted(_KNOWN_BARE_LINEAGE)}. "
        "It may SHRINK (route one through outbox.publish_lineage_with_outbox and delete its entry) but it "
        "may not grow."
    )


# --------------------------------------------------------------------------------------------------
# 2. No dead config — every env var the chart injects is actually READ by some service
# --------------------------------------------------------------------------------------------------

# Envs consumed by a THIRD-PARTY container's own binary, never by first-party code. Each entry is a
# server we deploy but did not write (RustFS/OpenBao/Postgres/NATS/Dapr/OTel/Greptime/Vector/Ray/OpenFGA),
# so its absence from our source proves nothing. Anything NOT matching here is OURS and must be read.
#
# `HF_` is the huggingface_hub/transformers family. `HF_HOME` in particular is load-bearing rather
# than decorative: the Ray image runs with a read-only root filesystem, so HF's default
# `~/.cache/huggingface` is unwritable and every model download would fail — the chart repoints it at
# a writable `/cache/hf` volume (`chart/templates/rayservice.yaml`). It is read by the HF libraries,
# never by our code, so its absence from first-party source proves nothing. Note `HOME$` above is
# anchored and matches only a var named exactly HOME, which is why HF_HOME needed its own prefix.
_THIRD_PARTY_ENV = re.compile(
    r"^(DAPR_|OTEL_|PYTHON|PATH$|HOME$|HF_|BAO_|VAULT_|POSTGRES_|PG|NATS_|AWS_|RUSTFS_|RUST_|GREPTIME|"
    r"VECTOR_|OPENFGA_|RAY_|MINIO_|S3_|TZ$|LANG$|LC_|UV_|VIRTUAL_ENV)"
)


def _chart_injected_envs() -> set[str]:
    envs: set[str] = set()
    for tpl in (CHART / "templates").rglob("*.yaml"):
        for m in re.finditer(r"name:\s*([A-Z][A-Z0-9_]{3,})\b", tpl.read_text()):
            name = m.group(1)
            if not _THIRD_PARTY_ENV.match(name):
                envs.add(name)
    return envs


def _chart_template_reads(text: str) -> str:
    """A chart template with its env-INJECTION lines removed, leaving only what could be a READ.

    `- { name: FOO, value: ... }` and `- name: FOO` are declarations; an env is only "read" here if it
    appears somewhere else in the template — inside an embedded script. Dropping the declarations is
    what keeps the dead-env guard able to fail.
    """
    import re as _re

    stripped = _re.sub(r"^\s*-?\s*\{?\s*name:\s*[A-Z0-9_]+.*$", "", text, flags=_re.MULTILINE)
    return _re.sub(r"^\s*-\s*name:\s*[A-Z0-9_]+\s*$", "", stripped, flags=_re.MULTILINE)


def _first_party_source() -> str:
    """ALL first-party source — python services AND the SvelteKit frontend.

    Deliberately wider than services/: the BFF reads LINEAGE_API in TypeScript, so a services-only search
    would call a live, load-bearing env "dead". Searching every first-party file makes the guard STRONGER
    (it now covers the frontend too) rather than weaker. Pydantic reads envs via alias="FOO", so a plain
    substring match over source is the right check — not os.environ lookups.
    """
    parts = [p.read_text(errors="ignore") for p in SERVICES.rglob("*.py")]
    # The shared platform lib is first-party but lives OUTSIDE services/ after the rask merge
    # (packages/service-kit, packages/ratch). service_kit/media/config.py is where most
    # MEDIA_*/LANCE_* envs are actually read, so omitting packages/ would report live config as dead.
    pkgs = REPO / "packages"
    if pkgs.exists():
        parts += [p.read_text(errors="ignore") for p in pkgs.rglob("*.py")]
    # The model runners (runners/<name>/) are first-party too — the chart injects their env
    # (ASSIST_FRAME_BASE) and only runner code reads it, so excluding them would flag live
    # config as dead.
    runners = REPO / "runners"
    if runners.exists():
        parts += [p.read_text(errors="ignore") for p in runners.rglob("*.py")]
    fe = REPO / "frontend"
    if fe.exists():
        for ext in ("*.ts", "*.svelte", "*.js"):
            parts += [p.read_text(errors="ignore") for p in fe.rglob(ext) if "node_modules" not in p.parts and ".svelte-kit" not in p.parts]
    # Chart templates can EMBED a first-party script that reads an env — `bootstrap-admin.yaml` runs an
    # inline python bootstrap that does `os.environ.get("FGA_SERVICE_READERS", "")`. Without this the
    # guard called a genuinely-consumed var dead, which is a false positive on a test whose whole job is
    # to be trusted.
    #
    # THE INJECTION LINES ARE STRIPPED FIRST, and that is the load-bearing half. A template names every
    # env it injects (`- name: FOO`), so scanning templates raw would make every var match its own
    # injection site and this test could never fail again — a guard that cannot fail is worse than no
    # guard, because it reads as coverage. Only the REMAINDER (the script bodies) counts as a read.
    parts += [_chart_template_reads(p.read_text(errors="ignore")) for p in (REPO / "chart" / "templates").rglob("*.yaml")]
    return "\n".join(parts)


def test_no_dead_chart_env_vars() -> None:
    """A chart-injected env that NO first-party code reads = a feature configured but INERT.

    This is exactly how MEDALLION_LINEAGE_OUTBOX_URI shipped: the chart set it, the producer never read
    it, and the outbox silently did nothing on that path while the docs claimed coverage. The feature was
    fully "configured" and completely dead.
    """
    source = _first_party_source()
    # A BARE substring match reports MEDALLION_RAY_ADDRESS as live when the only occurrence in source
    # is MEDALLION_RAY_ADDRESS_TYPO — measured: renaming that alias to a SUFFIXED name kept this test
    # green while the chart-injected var went unread. Require the name not to continue into another
    # identifier character, so being a prefix of a longer name no longer satisfies it.
    dead = sorted(env for env in _chart_injected_envs() if not re.search(rf"{re.escape(env)}(?![A-Za-z0-9_])", source))
    assert not dead, (
        f"the chart injects these env vars but NO first-party code reads them (dead config → a feature "
        f"that is configured but inert): {dead}. Either wire them up or delete them from the chart."
    )


# --------------------------------------------------------------------------------------------------
# 2b. No UNWIRED config — the MISSING DIRECTION of the guard above
# --------------------------------------------------------------------------------------------------


class _EnvSetting(NamedTuple):
    """One pydantic-settings field whose falsy default is a control-flow SWITCH.

    ``envs`` is every environment variable that can fill it (an ``alias``, each name in an
    ``AliasChoices``, or the ``env_prefix`` + FIELD_NAME fallback pydantic derives when a field
    declares no alias). ``guards`` names the identifiers whose truthiness the code actually branches
    on, so a failure message can point at the branch rather than assert one exists.
    """

    envs: tuple[str, ...]
    where: str
    default: str
    guards: tuple[str, ...]


#: Where a first-party `BaseSettings` subclass may live. `runners/` is included because a sealed
#: runner reads chart-injected env too (`ASSIST_FRAME_BASE`), and a runner is exactly the kind of
#: component whose config nobody remembers to render.
_SETTINGS_ROOTS: Final = (SERVICES, REPO / "packages", REPO / "runners")


def _settings_trees() -> dict[str, ast.Module]:
    """Parsed first-party Python, keyed by repo-relative path.

    A sealed runner keeps its OWN `.venv` inside the tree, so a scan rooted above one would walk a
    third-party site-packages — the same reason `_hierarchy_edge_call_sites` filters it. Read as
    bytes so a file carrying a BOM parses.
    """
    trees: dict[str, ast.Module] = {}
    for root in _SETTINGS_ROOTS:
        if not root.exists():
            continue
        for py in sorted(root.rglob("*.py")):
            posix = py.relative_to(REPO).as_posix()
            if ".venv/" in posix or "/node_modules/" in posix or "/tests/" in posix or "/test_" in posix:
                continue
            try:
                trees[posix] = ast.parse(py.read_bytes())
            except SyntaxError:
                continue
    return trees


def _leaf_name(node: ast.expr) -> str | None:
    """The identifier a `settings.foo` / `foo` expression ends in, else None."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _settings_class_names(trees: dict[str, ast.Module]) -> set[str]:
    """Every class that transitively subclasses `BaseSettings`, by NAME.

    Resolved by base-class name across the whole estate rather than by import, because the estate's
    settings hierarchies cross modules (`catalog.Settings` -> `GovernedAuthSettings` -> `BaseSettings`).
    Restricting to these is what keeps ordinary pydantic MODELS out: `lineage_kit.schemas` gives its
    fields camelCase wire aliases (`errorMessage`, `dataSource`), which are JSON field names and not
    environment variables at all.
    """
    bases: dict[str, set[str]] = {}
    for tree in trees.values():
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            bases.setdefault(cls.name, set()).update(ast.unparse(b).rsplit(".", 1)[-1] for b in cls.bases)

    def derives(name: str, seen: set[str]) -> bool:
        if name in seen:
            return False
        seen.add(name)
        return any(base == "BaseSettings" or derives(base, seen) for base in bases.get(name, ()))

    return {name for name in bases if derives(name, set())}


def _truthiness_guarded_names(trees: dict[str, ast.Module]) -> set[str]:
    """Identifiers whose TRUTHINESS decides control flow somewhere in first-party code.

    `if x`, `while x`, `x if … else`, `not x`, `x and y`, `assert x`, a comprehension's `if x`, and
    `bool(x)`/`any(x)`/`all(x)`. A COMPARISON is deliberately not collected: `if x == "dapr"` reads a
    value, it does not ask whether one was supplied, and only the latter is this gate's subject.
    """
    guarded: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            tested: list[ast.expr] = []
            if isinstance(node, (ast.If, ast.While, ast.IfExp)):
                tested = [node.test]
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                tested = [node.operand]
            elif isinstance(node, ast.BoolOp):
                tested = list(node.values)
            elif isinstance(node, ast.Assert):
                tested = [node.test]
            elif isinstance(node, ast.comprehension):
                tested = list(node.ifs)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("bool", "any", "all"):
                tested = list(node.args)
            for expr in tested:
                if (name := _leaf_name(expr)) is not None:
                    guarded.add(name)
    return guarded


def _keyword_flow(trees: dict[str, ast.Module]) -> dict[str, set[str]]:
    """`{source identifier -> every keyword-parameter name it is passed as}`.

    The one hop that matters, and without it this gate cannot see its own motivating bug. The catalog
    branches on nothing: it passes `settings.lineage_outbox_uri` into
    `publish_lineage_with_outbox(outbox_uri=…)`, and the branch (`staged = bool(outbox_uri)`) lives in
    `service_kit.lakehouse.outbox`, one package away and under a different name. Matching the KEYWORD
    is what carries the field to the branch that decides whether the feature happens.
    """
    flow: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg and (source := _leaf_name(kw.value)) is not None:
                    flow.setdefault(source, set()).add(kw.arg)
    return flow


_ENV_NAME: Final = re.compile(r"[A-Z][A-Z0-9_]*")
_ENV_PREFIX: Final = re.compile(r"env_prefix=['\"]([A-Z][A-Z0-9_]*)['\"]")


def _field_env_names(prefix: str | None, field: str, value: ast.expr) -> tuple[str, ...]:
    """Every env var that can fill one settings field.

    UPPER_SNAKE only: `MedallionSettings.transform` also answers to a lowercase `"transform"` alias,
    which is a payload key rather than an environment variable and no chart would ever render it.
    """
    aliases: list[str] = []
    if isinstance(value, ast.Call):
        fn = value.func
        if (fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)) == "Field":
            for kw in value.keywords:
                if kw.arg not in ("alias", "validation_alias"):
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    aliases.append(kw.value.value)
                elif isinstance(kw.value, ast.Call):  # AliasChoices("A", "B")
                    aliases += [a.value for a in kw.value.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    if not aliases and prefix is not None:
        aliases = [f"{prefix}{field.upper()}"]
    return tuple(a for a in aliases if _ENV_NAME.fullmatch(a))


def _falsy_default(value: ast.expr) -> str | None:
    """The field's default rendered for a message when that default is FALSY, else None.

    `default_factory=list/dict/set` counts (an empty collection is the same silence as `""`), and so
    does a `SecretStr("")`-shaped wrapper around an empty literal — the estate spells an optional
    secret that way, and reading only bare literals would let every one of them past.
    """
    if not isinstance(value, ast.Call):
        return None
    fn = value.func
    if (fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)) != "Field":
        return None
    for kw in value.keywords:
        if kw.arg == "default_factory":
            source = ast.unparse(kw.value)
            return f"{source}()" if source in ("list", "dict", "set") else None
        if kw.arg == "default":
            if isinstance(kw.value, ast.Call) and kw.value.args and isinstance(arg := kw.value.args[0], ast.Constant) and arg.value in ("", 0, False, None):
                return ast.unparse(kw.value)
            try:
                literal = ast.literal_eval(kw.value)
            except (ValueError, TypeError, SyntaxError):
                return None
            return repr(literal) if not literal else None
    return None


def _inert_if_absent_settings() -> list[_EnvSetting]:
    """Every settings field whose default is falsy AND whose falsiness steers the code.

    Both halves are load-bearing. A falsy default alone says nothing — `max_concurrent_writes=0`
    disables a limiter on purpose. A truthiness branch alone says nothing either — a field with a
    working default is never absent. Together they name the one shape that fails silently: the value
    is missing, some `if` takes the other road, and the process reports success while doing less.
    """
    trees = _settings_trees()
    settings_classes = _settings_class_names(trees)
    guarded = _truthiness_guarded_names(trees)
    flow = _keyword_flow(trees)

    found: list[_EnvSetting] = []
    for posix, tree in trees.items():
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            if cls.name not in settings_classes:
                continue
            prefix: str | None = None
            for stmt in cls.body:
                is_model_config = isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets)
                if is_model_config and (match := _ENV_PREFIX.search(ast.unparse(stmt.value))):
                    prefix = match.group(1)
            for stmt in cls.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name) or stmt.value is None:
                    continue
                envs = _field_env_names(prefix, stmt.target.id, stmt.value)
                default = _falsy_default(stmt.value)
                if not envs or default is None:
                    continue
                field = stmt.target.id
                guards = tuple(sorted(({field} | flow.get(field, set())) & guarded))
                if guards:
                    found.append(_EnvSetting(envs, f"{posix}::{cls.name}.{field}", default, guards))
    return sorted(found)


def _chart_declared_envs() -> set[str]:
    """Every env name a chart template names as CONFIG — an env-list entry or a ConfigMap/Secret key.

    Two patterns because the chart injects env two ways and a scan that knows one of them reports the
    other as unwired: `- { name: FOO, value: … }` in a container's `env`, and a bare `FOO: …` data key
    in `configmap.yaml`, delivered by `envFrom`. `*.tpl` is walked with `*.yaml` for the same reason —
    `rask.rayAuthEnv` emits `RAY_AUTH_TOKEN` from `_helpers.tpl` and nothing else in the chart names it.

    Only the RENDER can say whether a declaration actually fires; this set says the operator HAS a
    switch, which is what separates a deliberately-off feature from one nobody wired.
    """
    envs: set[str] = set()
    for tpl in sorted(list((CHART / "templates").rglob("*.yaml")) + list((CHART / "templates").rglob("*.tpl"))):
        text = tpl.read_text()
        envs.update(re.findall(r"name:\s*([A-Z][A-Z0-9_]{2,})\b", text))
        envs.update(re.findall(r"^\s{2,}([A-Z][A-Z0-9_]{2,}):\s", text, flags=re.MULTILINE))
    return envs


def _chart_rendered_envs() -> set[str]:
    """Every env the DEFAULT render actually delivers into a container.

    `envFrom` is resolved, not skipped: `RASK_DOCS`, `RASK_DAPR_ENABLED` and the notifications topics
    reach their pods as ConfigMap keys through `envFrom.configMapRef`, so a reader of `env[]` alone
    calls three live vars dead. Empty when helm is unavailable — the gate then decides on declarations
    alone, which is weaker (it cannot see a declaration whose values toggle never fires) but never
    wrong, since every first-party env the chart renders is named by one of its own templates.
    """
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        return set()
    argv = [helm, "template", "rask", str(CHART), "--set", "image.localImages=true"]
    argv += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    argv += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
    docs = [doc for doc in yaml.safe_load_all(out) if isinstance(doc, dict)]
    bundles = {
        (doc["kind"], (doc.get("metadata") or {}).get("name")): set(doc.get("data") or {}) | set(doc.get("stringData") or {})
        for doc in docs
        if doc.get("kind") in ("ConfigMap", "Secret")
    }

    def containers(node: Any) -> Iterator[dict[str, Any]]:
        if isinstance(node, dict):
            for key in ("containers", "initContainers"):
                yield from (c for c in node.get(key) or [] if isinstance(c, dict))
            for value in node.values():
                yield from containers(value)
        elif isinstance(node, list):
            for value in node:
                yield from containers(value)

    rendered: set[str] = set()
    for doc in docs:
        for container in containers(doc):
            rendered.update(e["name"] for e in container.get("env") or [] if isinstance(e, dict) and isinstance(e.get("name"), str))
            for source in container.get("envFrom") or []:
                if not isinstance(source, dict):
                    continue
                for ref, kind in (("configMapRef", "ConfigMap"), ("secretRef", "Secret")):
                    if name := (source.get(ref) or {}).get("name"):
                        rendered |= bundles.get((kind, name), set())
    return rendered


#: Inert-if-absent settings the chart deliberately does not set, and WHY absence is the right state.
#:
#: Three honest reasons recur here, and none of them is "we forgot". A DERIVE — the falsy value means
#: "compute it", not "skip it" (`registry_root`, `model_artifacts_root`, `clip_source_origin`), so
#: rendering it would pin a second spelling of a value two components already agree on. An OVERRIDE —
#: the estate's shared credential is the real path and the per-service variable exists to pin one
#: service off it. A SAFE OFF — the feature must not be on until something else lands, and the chart
#: staying silent is the mechanism, not an oversight.
#:
#: An entry here is a claim that the SHIPPED release is correct without the variable. It is not a
#: parking space: a setting whose absence loses work belongs in `_UNWIRED_DEBT` below.
_UNWIRED_BY_DESIGN: Final[dict[str, str]] = {
    # DERIVE. `Settings.registry_root` is `control_root or root`, so an unset control root puts the
    # warehouse registry in the catalog's own bucket — which is where a single-bucket estate wants it.
    "LANCE_CONTROL_ROOT": "empty derives the registry root from LANCE_REST_ROOT",
    # SAFE OFF, and LOUD rather than silent: only the `http` transport reads this, the chart renders
    # `LANCE_LINEAGE_TRANSPORT=dapr` (services.yaml), and `_validate_lineage` REFUSES TO BOOT on
    # http-with-no-url. There is no configuration in which the absence quietly does less.
    "LANCE_LINEAGE_URL": "the chart selects the dapr transport; the http transport fails closed without this",
    # DERIVE. `model_artifacts_root` reconstructs the trainer's own layout from `models_root`, and the
    # two are a byte-for-byte mirror on purpose — a rendered value is a second place for them to drift.
    "LANCE_MODEL_ARTIFACTS_ROOT": "empty derives the artifact tree from LANCE_MODELS_REGISTRY_ROOT",
    # OVERRIDE, and the weaker door of the two. The catalog verifies OIDC JWTs, so a static bearer is
    # not an identity there; the estate's answer is the service door, and the chart renders
    # `MEDALLION_CATALOG_SERVICE_IDENTITY` for the producer and every mover (medallion.yaml).
    "MEDALLION_CATALOG_TOKEN": "superseded by the service-identity door, which the chart does render",
    # DERIVE. `catalog_table_id` falls back to the dataset id, which is the identifier the annotation
    # tables are already addressed by.
    "MEDIA_CATALOG_NAMESPACE": "unset names annotation tables by their dataset id",
    # SAFE OFF, and rendering it would BREAK the estate's rule rather than fix anything: the secret
    # half comes from the Dapr store fail-closed (media/config.py `storage_options`), and a plaintext
    # secret in pod env is the exact shape `feedback-secrets-via-secret-store-only` forbids.
    "MEDIA_S3_SECRET_ACCESS_KEY": "the secret comes from the Dapr store; a rendered value would be an env-borne credential",
    # OVERRIDE. `IngestSettings.catalog_app_token` is `catalog_app_token_override or app_api_token`,
    # and the chart renders the shared `APP_API_TOKEN`. Absent means "use the estate credential".
    "RASK_CATALOG_APP_TOKEN": "falls back to the shared APP_API_TOKEN the chart renders",
    "RASK_LINEAGE_APP_TOKEN": "falls back to the shared APP_API_TOKEN the chart renders",
    # SAFE OFF. `register_middleware` installs CORSMiddleware only for a non-empty list, and every zone
    # reaches `/api` same-origin through the gateway — a rendered origin list would only widen what a
    # browser is told to trust, for no caller that exists.
    "RASK_CORS_ORIGINS": "no cross-origin caller exists; the fleet is reached same-origin through the gateway",
    # SAFE OFF, and setting it would REINTRODUCE a known silent loss. A bearer api key does not
    # authenticate a service at rask's ingest — `lineage.api.security` opens the service door on
    # `dapr-api-token` + `x-lance-service-identity`, so a key 401s and `ClientEmitter` swallows it.
    # The chart wires the pair that works (`LINEAGE_SERVICE_TOKEN` / `LINEAGE_SERVICE_ID`).
    "RASK_LINEAGE_API_KEY": "the service door takes a token+identity pair, not a bearer key; a key would 401 silently",
    # SAFE OFF, and it must STAY off until a different artefact changes. daprd rejects the whole
    # transactional upsert when `ttlInSeconds` arrives with the `ActorStateTTL` feature disabled, and
    # the chart's one Dapr `Configuration` carries no `features:` block — so rendering this true ahead
    # of that block would fail EVERY inbox write. The compaction reminder is the authoritative bound.
    "RASK_NOTIFICATIONS_ACTOR_STATE_TTL_ENABLED": "must not precede a Dapr Configuration carrying features:[ActorStateTTL]",
    # DERIVE. `clip_source_origin` builds the pod's own loopback from `service_port`; the override is
    # for a deployment that fronts the media route elsewhere.
    "VIEWER_CLIP_SOURCE_ORIGIN": "empty derives the pod's own loopback origin from VIEWER_SERVICE_PORT",
}

#: Inert-if-absent settings NOTHING in the chart sets, where the absence costs something.
#:
#: The same debt `MEDALLION_LINEAGE_OUTBOX_URI` was before it was wired, and the ratchet is the same
#: as `_KNOWN_BARE_LINEAGE`'s: this may SHRINK, it may not grow. Wiring one is a chart edit plus a
#: values key, so it is not a change to make blind — but the cost of leaving it is stated here rather
#: than nowhere, which is the whole difference between debt and a defect.
_UNWIRED_DEBT: Final[dict[str, str]] = {
    # Every STAGE job's own OpenLineage emission, off in every shipped release. `ray_submit` puts
    # `LINEAGE_URL` into the job's `runtime_env.env_vars` only when this is set, and a runner's
    # `emit()` returns False on an unset one — so a lane whose runner emits (the dummy runner does,
    # and any workload runner may) records nothing, while the run itself succeeds and every pod is
    # green. The sibling `MEDALLION_TRAIN_LINEAGE_URL` IS rendered (medallion.yaml), so the train lane
    # emits and the stage lane does not: one asymmetry between two adjacent settings, invisible to
    # every test that exercises either half on its own.
    "MEDALLION_STAGE_LINEAGE_URL": "stage-lane Ray jobs emit no OpenLineage at all; the train lane's twin IS rendered",
}


def test_every_inert_if_absent_setting_is_wired_or_declared_unwired_on_purpose() -> None:
    """The MISSING DIRECTION of `test_no_dead_chart_env_vars`: code reads an env NO chart sets.

    The guard above catches a chart var no code reads. Its reverse has no guard, and that is how BOTH
    `LANCE_LINEAGE_OUTBOX_URI` and `MAINTENANCE_LINEAGE_OUTBOX_URI` shipped: threaded through the
    service into the outbox publisher, rendered nowhere, so `publish_lineage_with_outbox` took its
    `staged = bool(outbox_uri)` branch to False and every release ran the emit as a plain publish. The
    feature was fully implemented, fully tested, and never once happened.

    THE RULE, and why it is this one. "Every setting must be rendered" is wrong — most defaults are
    correct, and a chart that restates them is a second place for them to drift. The honest line is
    narrower: a setting whose default makes the feature INERT must be rendered, or declared
    default-off on purpose. Mechanically, "inert" is the conjunction of two facts a parser can see:

      1. the default is FALSY — `""`, `None`, `False`, `0`, an empty collection, `SecretStr("")` — so
         an unconfigured deployment gets exactly the same value as a deployment that tried and failed;
      2. first-party code branches on that value's TRUTHINESS, directly or one keyword-argument hop
         away, so the falsiness is a switch rather than a datum.

    Either fact alone proves nothing. `max_concurrent_writes=0` is falsy and disables a limiter ON
    PURPOSE; `lineage_job_namespace` is branched on nowhere and has a working default. Together they
    name the one shape that fails silently — the value is missing, an `if` takes the other road, and
    the service reports success while doing less than it claims. Every such setting is then either
    wired (the default render sets it, or a template declares an injection site a values key controls)
    or it is named below with a reason, which is the estate's `_PUBLISH_INTENT` idiom: a classification
    a parser cannot make, made once by a human and re-checked on every run.

    WHAT THIS GATE STILL CANNOT SEE, stated rather than implied: a setting consumed WITHOUT a
    truthiness branch — interpolated straight into a URL, say, where empty yields a malformed request
    instead of a skipped one — is not a candidate here, and a second hop (field -> local -> keyword)
    is not followed. Both are false negatives; neither makes a reported violation wrong.
    """
    wired = _chart_rendered_envs() | _chart_declared_envs()
    declared = _UNWIRED_BY_DESIGN.keys() | _UNWIRED_DEBT.keys()
    undeclared = sorted(
        f"{s.envs[0]} ({s.where}, default={s.default}, branched on via {'/'.join(s.guards)})"
        for s in _inert_if_absent_settings()
        if not (set(s.envs) & wired) and not (set(s.envs) & declared)
    )
    assert not undeclared, (
        f"these settings disable a feature when unset and NOTHING in the chart sets them: {undeclared}. "
        "Each one is the LANCE_LINEAGE_OUTBOX_URI shape — the code is written, the branch is there, and "
        "no deployment ever takes it. Render it from the chart, or add it to `_UNWIRED_BY_DESIGN` with "
        "the reason the absent state is the correct one (a derive, an override, or a safe off), or to "
        "`_UNWIRED_DEBT` with what is silently lost until someone wires it."
    )


def test_the_unwired_registries_describe_settings_that_are_still_unwired() -> None:
    """A registry entry is a claim about TODAY's chart, so it expires when the chart changes.

    Both failures below are the same defect as a stale `_PUBLISH_INTENT` row: prose describing an
    estate that is not there. An entry for a variable the chart now renders reads as "we decided not
    to set this" beside a template that sets it, and an entry for a field that no longer branches on
    its default is a reason preserved for a mechanism that is gone.
    """
    wired = _chart_rendered_envs() | _chart_declared_envs()
    registries = {"_UNWIRED_BY_DESIGN": _UNWIRED_BY_DESIGN, "_UNWIRED_DEBT": _UNWIRED_DEBT}

    now_wired = sorted(f"{name}[{env}]" for name, registry in registries.items() for env in registry if env in wired)
    assert not now_wired, f"the chart now sets these, so the entry claiming it deliberately does not is false: {now_wired}. Delete the entry."

    candidates = {env for s in _inert_if_absent_settings() for env in s.envs}
    gone = sorted(f"{name}[{env}]" for name, registry in registries.items() for env in registry if env not in candidates)
    assert not gone, (
        f"these registered settings no longer disable anything when unset — the field, its falsy default or the branch on it is gone: {gone}. Delete the entry."
    )


# --------------------------------------------------------------------------------------------------
# 3. FGA schema contract — every relation the code WRITES or CHECKS must exist on that type
# --------------------------------------------------------------------------------------------------


def _model_relations() -> dict[str, set[str]]:
    model = json.loads((SERVICE_KIT / "governed/auth/model.json").read_text())
    return {t["type"]: set(t.get("relations") or {}) for t in model["type_definitions"]}


def _fga_literals() -> list[tuple[str, str, str]]:
    """(file:line, object_type, relation) for every literal FGA (type, relation) pair in the code.

    Catches the `warehouse#parent` class: a mocked `fga.check`/`write_tuples` asserts the STRING that was
    passed, never that the relation EXISTS on the type — so a phantom relation sails through every unit
    test and only fails at runtime, as an OpenFGA rejection (a fail-closed 503 for every caller).
    """
    found: list[tuple[str, str, str]] = []
    # relation="X", ... obj=f"type:{...}"  /  ClientTuple(relation="X", object=f"type:...")
    #
    # `parent_object=` / `parent_relation=` are EXCLUDED from this naive pairing, because in
    # `grant_on_create` the parent is the tuple's USER, not its object: the tuple is
    # (user=<parent_object>, relation=<parent_relation>, object=<resource>:<obj_id>). Pairing them
    # here reported `project#tenant`, which is not a relation anyone writes. They get their own,
    # STRICTER rule below — the relation must exist on the RESOURCE type.
    rel_re = re.compile(r'(?<!parent_)relation=["\']([a-z_]+)["\']')
    obj_re = re.compile(r'(?<!parent_)(?:obj|object)=f?["\']([a-z_]+):')
    for py in SERVICES.rglob("*.py"):
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            rel = rel_re.search(line)
            if not rel:
                continue
            window = "\n".join(lines[max(0, i - 4) : i + 5])
            for obj in obj_re.finditer(window):
                found.append((f"{py.relative_to(REPO)}:{i + 1}", obj.group(1), rel.group(1)))
    return found


def _parent_edge_literals() -> list[tuple[str, str, str]]:
    """(file:line, resource_type, parent_relation) for every `grant_on_create` parent edge.

    The edge's relation lives on the CHILD, so `parent_relation` must exist on `resource`. Every
    governed type spells it `parent`; `annotation_project` spells it `tenant`. Writing the wrong one
    produces a tuple OpenFGA ACCEPTS and no rule ever reads — the inheritance silently does not
    exist, and the object looks unowned by everyone including its creator. That failure is invisible
    to a mocked-client unit test, which is the same blind spot the sibling check above exists for.
    """
    found: list[tuple[str, str, str]] = []
    res_re = re.compile(r'resource=["\']([a-z_]+)["\']')
    prel_re = re.compile(r'parent_relation=["\']([a-z_]+)["\']')
    for py in SERVICES.rglob("*.py"):
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            prel = prel_re.search(line)
            if not prel:
                continue
            window = "\n".join(lines[max(0, i - 8) : i + 3])
            res = res_re.search(window)
            if res:
                found.append((f"{py.relative_to(REPO)}:{i + 1}", res.group(1), prel.group(1)))
    return found


def test_every_parent_edge_relation_exists_on_the_child_type() -> None:
    """The `annotation_project#tenant` class of bug, made mechanical."""
    model = _model_relations()
    phantom = [f"{loc} -> {res}#{rel}" for loc, res, rel in _parent_edge_literals() if res in model and rel not in model[res]]
    assert not phantom, (
        "a `grant_on_create` parent edge names a relation that does not exist on the child type: "
        f"{phantom}. The write SUCCEEDS and the inheritance silently does not exist — every rung on "
        "the object then denies for everyone, creator included."
    )


#: The ONLY production sites allowed to CALL :func:`service_kit.governed.fga.hierarchy_edge_tuples`,
#: and what each one is for.
#:
#: SCOPED TO THE ``parent`` EDGE, which is what this function mints and all this gate can speak for.
#: `grant_on_create` is where the catalog's create doors land — `fga_deps.seed_ownership` is the one
#: post-create seed and it reaches the edge through there — so a namespace or a table gets its
#: ``parent`` link from a single writer.
#:
#: A WAREHOUSE DOES NOT, and that is by design rather than an escape: its parent-pointer relation is
#: ``project``, not ``parent`` (`model.fga`), so `fga_deps.seed_warehouse` writes those tuples
#: directly — `grant_on_create` hardcodes ``parent``, a relation the ``warehouse`` type does not
#: define, and writing it makes OpenFGA reject the whole seed with a 503. So "one writer per object"
#: is true of the ``parent`` edge and false of hierarchy in general; this gate claims only the former.
#:
#: The medallion's train handler is the sanctioned second writer, and the reason is a fact about the
#: model plane rather than a convenience: `table:<models_ns>$<model>` has NO catalog record. The
#: trainer writes the registry dataset straight to its URI and the catalog's model doors open that
#: URI explicitly, so no create door ever runs for a model and `seed_ownership` never fires on one.
#: That write is the only thing that makes the FGA object exist.
#:
#: Anything else reaching for this function is a service minting hierarchy for objects some create
#: door already governs — two writers for one edge, which is how a parent link ends up asserted in
#: two shapes that drift. Route it through `seed_ownership`, or bring the reason here.
_HIERARCHY_EDGE_WRITERS: Final = {
    "packages/service-kit/src/service_kit/governed/fga.py",
    "services/medallion/src/medallion/services/train.py",
}


def _hierarchy_edge_call_sites() -> list[str]:
    """Repo-relative path of every production CALL to ``hierarchy_edge_tuples``.

    PARSED, not grepped. The pairing is discussed in prose at half a dozen sites — its own docstring,
    `grant_on_create`'s inline note, the seed script's shell helper, the comment at the medallion call
    site — so a text scan reports the discussion as writers and the gate would be measuring nothing.
    """
    found: set[str] = set()
    for root in (REPO / "packages", SERVICES, REPO / "scripts", REPO / "runners"):
        for py in root.rglob("*.py"):
            posix = py.relative_to(REPO).as_posix()
            # A sealed runner keeps its OWN `.venv` inside the tree (`runners/<w>/.venv`), so a scan
            # rooted above one walks a third-party site-packages — thousands of files, and the first
            # one carrying a BOM aborts the parse. Read as bytes for the same reason.
            if "/tests/" in posix or "/test_" in posix or ".venv/" in posix or "/node_modules/" in posix:
                continue
            for node in ast.walk(ast.parse(py.read_bytes())):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
                if name == "hierarchy_edge_tuples":
                    found.add(posix)
    return sorted(found)


def test_only_the_sanctioned_writers_seed_a_hierarchy_edge() -> None:
    """One create-door seed, plus the model plane's — which has no create door to seed it."""
    callers = _hierarchy_edge_call_sites()
    missing = sorted(_HIERARCHY_EDGE_WRITERS - set(callers))
    assert not missing, (
        f"these files are declared hierarchy-edge writers and call nothing: {missing}. Either the "
        "write moved and this gate is now vacuous, or a governed object lost its parent edge."
    )
    rogue = sorted(set(callers) - _HIERARCHY_EDGE_WRITERS)
    assert not rogue, (
        f"a third site calls hierarchy_edge_tuples: {rogue}. The ``parent`` edge is minted by the "
        "catalog's create-door seed (`fga_deps.seed_ownership` → `grant_on_create`); the medallion's "
        "train handler is the one other caller, because a model registry dataset has no catalog "
        "record and so no create door runs for it. (A warehouse's parent pointer is a different "
        "relation, ``project``, written directly by `seed_warehouse` and outside this gate.) A service that owns neither is asserting a parent link some "
        "create door also asserts — two writers for one edge, and nothing reports the drift. Seed it "
        "through the catalog, or record why it cannot be and add it to `_HIERARCHY_EDGE_WRITERS`."
    )


def test_every_fga_relation_in_code_exists_in_the_compiled_model() -> None:
    model = _model_relations()
    phantom = [f"{loc} -> {obj_type}#{rel}" for loc, obj_type, rel in _fga_literals() if obj_type in model and rel not in model[obj_type]]
    assert not phantom, (
        "the code writes/checks FGA relations that do NOT exist on that type in the compiled model.json — "
        f"OpenFGA REJECTS these at runtime (fail-closed 503 for every caller): {phantom}"
    )


def _relation_constants() -> list[tuple[str, str, str]]:
    """(file:line, CONST_NAME, value) for every module-level `*_RELATION` constant under services/.

    The literal scanner above pairs `relation="X"` with a nearby `obj=f"type:"`, which cannot see a
    relation held in a CONSTANT and passed POSITIONALLY. That is not a hypothetical shape — it is how
    the estate's most load-bearing relation is written:

        NOTIFY_RELATION: Final = "can_be_notified"          # visibility.py:60
        ...
        await self._filter(subject, names, NOTIFY_RELATION)  # :150, positional

    Measured 2026-08-22: renaming that constant's value to `can_be_notifiedX`, a relation the model
    does not define, left 774 tests passing. In production `can_be_notified` gates EVERY delivery in
    the plane and an FGA rejection is fail-closed, so the phantom does not degrade the inbox — it
    silences it, for every subject, while the suite stays green.

    TWO shapes are collected, because the naming convention alone leaves a hole. `services/viewer`
    declares `READ_METADATA`, `READ_DATA` and `BROWSE_STORAGE` (api/security.py:38,45,59) and passes
    them as `relation=READ_DATA` — no `_RELATION` suffix, no string literal, so the literal scanner and
    a name-only rule both miss all three. So:

    * any module-level constant NAMED `*_RELATION` — the convention, and the only way to reach one
      passed positionally, as `NOTIFY_RELATION` is; and
    * any module-level constant RESOLVED from a `relation=<IDENT>` keyword argument in the same file —
      which catches the viewer's three regardless of what they are called.

    Both stop at the file boundary: a constant imported from elsewhere and passed on is out of reach
    without dataflow, and is stated here rather than papered over.
    """
    found: list[tuple[str, str, str]] = []
    any_const = re.compile(r"""^([A-Z][A-Z_0-9]*)(?::\s*[A-Za-z]+)?\s*=\s*["']([^"']+)["']""")
    used_as_relation = re.compile(r"relation=([A-Z][A-Z_0-9]*)")
    sources = [(py, py.read_text().splitlines()) for py in SERVICES.rglob("*.py") if "/tests/" not in py.as_posix()]
    # ESTATE-WIDE, not per file. The viewer declares READ_DATA/READ_METADATA/BROWSE_STORAGE in
    # api/security.py and passes them as `relation=READ_DATA` from its ENDPOINT modules, so a
    # same-file reference set reached none of the three. Matching by name across the tree can only
    # over-collect (a same-named constant elsewhere gets validated too), and validating an extra
    # relation string is harmless; under-collecting is what this gate exists to stop.
    referenced = {n for _, lines in sources for line in lines for n in used_as_relation.findall(line)}
    for py, lines in sources:
        for i, line in enumerate(lines):
            m = any_const.match(line)
            if m and (m.group(1).endswith("_RELATION") or m.group(1) in referenced):
                found.append((f"{py.relative_to(REPO)}:{i + 1}", m.group(1), m.group(2)))
    return found


def test_every_relation_constant_names_a_relation_the_model_defines() -> None:
    """A `*_RELATION` constant whose value no type defines is a fail-closed outage, not a typo."""
    model = _model_relations()
    every_relation = {rel for rels in model.values() for rel in rels}
    assert every_relation, "the compiled model defined no relations — this gate would pass vacuously"

    constants = _relation_constants()
    assert constants, "no *_RELATION constants found under services/ — the scan root or the naming convention moved, and this gate is now vacuous"
    phantom = [f"{loc} -> {name} = {value!r}" for loc, name, value in constants if value not in every_relation]
    assert not phantom, (
        "these constants name relations that exist on NO type in the compiled model.json. OpenFGA "
        f"rejects them at runtime and the check fails CLOSED, so every caller is denied: {phantom}"
    )


def _helm_template(*set_values: str) -> str:
    """Render the chart, skipping the test if helm is not on PATH or in .localbin."""
    import shutil
    import subprocess

    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", str(CHART)]
    # Side-loaded images: see `rask.image` in _helpers.tpl — the chart refuses a bare
    # `<component>:<tag>` unless this is set, because that is docker.io and not a local image.
    argv += ["--set", "image.localImages=true"]
    # The identity values every render needs since auth defaults ON (2026-08-06). The chart refuses
    # OIDC without a session secret ON PURPOSE — that refusal is what stops a forgotten values file
    # installing an ungoverned estate, and it is asserted directly by
    # `test_the_chart_REFUSES_to_render_oidc_without_a_session_secret`. Supplying dev values HERE
    # keeps every other render test testing its own subject rather than re-testing the guard.
    argv += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    argv += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    for value in set_values:
        argv += ["--set", value]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


def test_every_first_party_deployment_is_hardened() -> None:
    """The docs claim "every Deployment has probes + preStop". The gateway had NEITHER (audit 2026-07-14).

    An "every" claim in prose is worth nothing; this loop is what makes it true. It renders the chart and
    checks each FIRST-PARTY Deployment (third-party subcharts — dapr/nats/openfga/dex — are not ours to
    template). preStop matters most on the gateway: it is the INGRESS, so without a drain delay a rolling
    update drops in-flight requests while kube-proxy is still routing to the terminating pod.

    IT NO LONGER NAMES ITS OWN SUBJECTS. This carried a hand-written tuple of ten name fragments —
    gateway, catalog, lineage, compaction, medallion-producer, the three movers, web, notifications —
    which omitted controlplane, compute, flows, ingest, maintenance, viewer, search and annotator. So a
    gate whose docstring argues that "an every claim in prose is worth nothing" made exactly that kind
    of claim with a literal list, and controlplane shipped with no preStop at all: a first-party
    Deployment serving `/api/projects` through the gateway, dropping in-flight project reads on every
    `helm upgrade` for as long as kube-proxy took to notice the endpoint removal.

    It now derives its subjects from the render (`_first_party_deployments`), so a NEW Deployment is
    checked by default rather than invisible until somebody remembers it. Per CONTAINER, too — the
    tuple version matched on the doc text, so a second container in a pod could satisfy the check for
    the first.
    """
    unhardened: list[str] = []
    for doc in _first_party_deployments(_rendered_docs("explorer.enabled=true")):
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            missing = [
                key
                for key, present in (
                    ("livenessProbe", "livenessProbe" in container),
                    ("readinessProbe", "readinessProbe" in container),
                    ("preStop", bool((container.get("lifecycle") or {}).get("preStop"))),
                )
                if not present
            ]
            if missing:
                unhardened.append(f"{name}/{container['name']} missing {missing}")
    assert not unhardened, f"first-party Deployments are not hardened: {unhardened}"


def test_ingress_holds_a_live_stream_open_longer_than_nginx_default() -> None:
    """A `query.live` stream dies at the edge, silently, unless the Ingress overrides the read timeout.

    ingress-nginx's default `proxy_read_timeout` is 60s and it measures IDLE time on the upstream
    connection, so a live query that yields only when its data changes — the discipline the lakehouse
    zone's `controlEvents` generator follows — is cut off after a minute of quiet. SvelteKit's SSE
    transport adds no keepalive to refresh that clock (kit 2.70.1 `runtime/server/remote.js` enqueues
    the payload and nothing else), so the browser reconnects every 60s and re-runs the generator from
    its first poll. That is more traffic than the `setInterval` it replaced, while looking live.

    Rendered, not asserted in prose: the annotation must be present AND longer than the 60s default,
    or the override is decoration.
    """
    rendered = _helm_template("ingress.enabled=true")
    ingress = next((doc for doc in rendered.split("\n---") if re.search(r"^kind: Ingress$", doc, re.MULTILINE)), None)
    assert ingress is not None, "ingress.enabled=true rendered no Ingress"
    m = re.search(r"nginx\.ingress\.kubernetes\.io/proxy-read-timeout:\s*\"?(\d+)\"?", ingress)
    assert m, (
        "the Ingress carries no nginx.ingress.kubernetes.io/proxy-read-timeout — every live stream "
        "through the edge is severed after nginx's 60s default, and SvelteKit sends no keepalive"
    )
    assert int(m.group(1)) > 60, (
        f"proxy-read-timeout is {m.group(1)}s, which is not longer than nginx's 60s default — the annotation is present but changes nothing"
    )


@pytest.mark.parametrize(
    ("obj_type", "relation"),
    [
        # the exact pairs whose absence caused live outages / dead gates
        ("warehouse", "project"),  # the parent pointer (NOT `parent` — that was the 503)
        ("warehouse", "owner"),
        ("project", "can_create_warehouse"),  # the dormant admin gate #3-A finally enforces
        ("namespace", "parent"),
        ("table", "parent"),
    ],
)
def test_load_bearing_relations_are_defined(obj_type: str, relation: str) -> None:
    assert relation in _model_relations()[obj_type], f"{obj_type}#{relation} is load-bearing but missing from the compiled model"


def test_every_helm_set_key_in_our_scripts_exists_in_values() -> None:
    """A `--set` key that does not exist in values.yaml is a SILENT no-op — helm accepts it without a word.

    The bug this encodes (2026-07-14): `scripts/e2e_stack.sh` passed `--set web.enabled=false` to deploy a
    headless stack. There was no `web.enabled` key. Helm shrugged, the web Deployment (which had no `if`
    guard at all) rendered anyway, its image is never built in CI, so it sat in ImagePullBackOff and
    `helm --wait` could never converge. The e2e-stack job — the entire point of P0.1, the job whose whole
    purpose is to stop us shipping unproven claims — therefore FAILED ON EVERY RUN and nobody noticed.

    A flag you *believe* you are setting, that silently sets nothing, is worse than no flag: it makes a
    stack you never actually configured look configured. This asserts every key we --set actually exists.
    """
    import yaml

    values = yaml.safe_load((CHART / "values.yaml").read_text())

    def defined(dotted: str) -> bool:
        node = values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    # `--set a.b=c` / `--set-json a.b=[...]` / `--set a.b=c,d.e=f` across every script we ship.
    pattern = re.compile(r"--set(?:-json|-string)?[= ]\"?([A-Za-z0-9_.\[\]-]+)=")
    unknown: list[str] = []
    for script in sorted((REPO / "scripts").glob("*.sh")):
        for key in pattern.findall(script.read_text()):
            base = key.split("[")[0]  # list index: catalog.multibase.dataBases[0] -> ...dataBases
            if not defined(base):
                unknown.append(f"{script.name}: --set {key} (no such key in values.yaml)")
    assert not unknown, "helm --set keys that silently do nothing:\n  " + "\n  ".join(unknown)


def test_no_warehouse_bucket_access_bypasses_the_deactivation_gate() -> None:
    """SECURITY INVARIANT (audit #2/#6 + #35 class): reaching a warehouse's isolated bucket connection —
    which happens only via ``namespace_for_root`` — MUST consult the warehouse's lifecycle status, or a
    handler can provision/read inside a QUARANTINED (deactivated) bucket, bypassing tenant offboarding.

    Today two paths reach a bucket: ``get_namespace`` (through ``_resolve_warehouse_root``'s live status
    gate) and ``create_warehouse_namespace`` (which checks ``record["status"]`` inline). This test fails the
    moment a NEW caller of ``namespace_for_root`` appears in a module that does not also gate on status —
    exactly the bug the audit found in the namespace-create path.
    """
    # Match the cached wrapper `namespace_for_root(` but NOT the raw builder `build_namespace_for_root(`
    # (the wrapper's substring lives inside the builder's name) — a word boundary before the underscore.
    caller_re = re.compile(r"(?<![A-Za-z_])namespace_for_root\(")
    ungated: list[str] = []
    for path in SERVICES.rglob("*.py"):
        text = path.read_text()
        if not caller_re.search(text):
            continue
        # The definition site (dependencies.py) gates via _resolve_warehouse_root; every caller must gate on
        # the warehouse status one way or another before it reaches the bucket.
        gated = "_resolve_warehouse_root" in text or "warehouse_status" in text or re.search(r'\.get\("status"\)|\["status"\]', text) is not None
        if not gated:
            ungated.append(str(path.relative_to(REPO)))
    assert not ungated, (
        "these modules reach a warehouse bucket via namespace_for_root WITHOUT a deactivation-status gate "
        f"— a quarantined-warehouse bypass (audit #2/#6): {ungated}"
    )


def test_catalog_authz_primitive_fails_closed_on_openfga_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """SECURITY INVARIANT (condition 3a): the shared catalog authz primitive ``_require`` — which EVERY
    catalog gate (``authorize`` / ``require_*``) funnels through to ``fga.check`` — must RAISE (fail closed)
    when OpenFGA is unreachable, never swallow the outage and allow. If it failed OPEN, every gated route
    would too. (The lineage read gate's fail-closed is pinned in test_lineage_auth.py.)
    """
    import asyncio
    from unittest.mock import MagicMock

    from lance_namespace import ServiceUnavailableError

    from catalog.api import fga_deps as cat_fga

    async def _outage(*_a: object, **_k: object) -> bool:
        raise ServiceUnavailableError("openfga down")

    # Patch the fga module AS SEEN BY fga_deps (the consuming module), so the outage reaches
    # the exact `fga.check` reference `_require` calls.
    monkeypatch.setattr(cat_fga.fga, "check", _outage)
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(cat_fga._require(MagicMock(), user="u", relation="can_read_data", obj="table:x"))


def test_authz_decisions_are_audited() -> None:
    """Compliance invariant (#41): the single authz choke point ``fga_deps._require`` must emit an audit
    event, so every governed access decision — allow and deny — lands on the dedicated audit trail. Before
    #41 only denials were logged (to the general logger). Grep-provable so it can never silently regress:
    the moment ``_require`` stops calling ``audit(``, a governed deployment's audit trail goes half-blind."""
    src = (_svc("catalog") / "api" / "fga_deps.py").read_text()
    body = src.split("async def _require(", 1)[1].split("\nasync def ", 1)[0]
    assert "audit(" in body, "_require must emit an audit event for every authz decision (#41 compliance)"


def test_batch_authz_and_credential_vending_are_audited() -> None:
    """Compliance invariant (#41 follow-up): the two authz surfaces that do NOT funnel through ``_require``
    — the batch gate and the credential vend — must emit their own audit events, or their decisions fall
    off the trail exactly the way the pre-#41 code's did. Grep-provable like the ``_require`` guard."""
    fga_src = (_svc("catalog") / "api" / "fga_deps.py").read_text()
    batch = fga_src.split("async def _authorize_batch(", 1)[1].split("\nasync def ", 1)[0]
    assert batch.count("audit(") >= 3, "_authorize_batch must audit table/parent/owner decisions (#41)"
    vend = (_svc("catalog") / "api" / "v1" / "endpoints" / "credentials.py").read_text()
    assert vend.count("audit(") >= 2, "credential vending must audit the write-tier gate + issuance (#41)"


# --------------------------------------------------------------------------------------------------
# 4. Event-fabric contract (DATA-CONTRACT §7) — topics are pinned constants, never inline literals
# --------------------------------------------------------------------------------------------------

#: The exact topic constants the event fabric runs on (DATA-CONTRACT §7.2). The topic NAME is the
#: compatibility unit — a consumer subscribed to `lineage.events.v1` is entitled to that payload shape
#: forever — so a rename or version bump must be a deliberate act that also updates this pin (and, for a
#: breaking change, ships a NEW `.vN` topic with parallel consumers), never a drive-by edit.
_PINNED_TOPICS: list[tuple[str, str]] = [
    # the two cross-plane topics carry an explicit .v1 (the versioned compatibility unit)
    ("packages/service-kit/src/service_kit/control_events.py", 'CONTROL_TOPIC = "catalog.control.v1"'),
    ("services/lineage/src/lineage/core/config.py", 'default="lineage.events.v1", alias="LINEAGE_DAPR_TOPIC"'),
    ("services/catalog/src/catalog/core/config.py", 'default="lineage.events.v1", alias="LANCE_DAPR_TOPIC"'),
    ("services/maintenance/src/maintenance/core/config.py", 'default="lineage.events.v1", alias="MAINTENANCE_LINEAGE_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="lineage.events.v1", alias="MEDALLION_LINEAGE_TOPIC"'),
    # The medallion's publication head rides the CATALOG's control topic, so it IMPORTS the producer's
    # constant rather than re-typing the name — what catalog + maintenance already do. Pinned as the
    # import AND the use: either alone can be satisfied while the other re-literalizes the string.
    ("services/medallion/src/medallion/core/config.py", "from service_kit.control_events import CONTROL_TOPIC"),
    ("services/medallion/src/medallion/core/config.py", 'default=CONTROL_TOPIC, alias="MEDALLION_CONTROL_TOPIC"'),
    # the intra-cascade trigger topics (unversioned by design: both ends deploy atomically from one chart)
    ("services/medallion/src/medallion/core/config.py", 'default="medallion.bronze", alias="MEDALLION_SUB_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="medallion.bronze", alias="MEDALLION_BRONZE_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="training.jobs", alias="MEDALLION_TRAIN_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="medallion.media", alias="MEDALLION_MEDIA_TOPIC"'),
    # The inbox is the SECOND consumer of the run-lifecycle topic, and the one that reads it as a
    # notification rather than as graph input — so the name it subscribes is the same compatibility
    # unit, pinned in the same act.
    ("services/notifications/src/notifications/api/settings.py", 'default="lineage.events.v1", alias="RASK_NOTIFICATIONS_LINEAGE_TOPIC"'),
    # the stream bindings the topics land on (nats-stream-job) + the DLQ parking subjects
    ("chart/templates/nats-stream-job.yaml", 'add_if_missing CATALOG_CONTROL "catalog.control.>"'),
    ("chart/templates/nats-stream-job.yaml", 'add_if_missing DLQ "dlq.>"'),
    ("chart/templates/services.yaml", 'LINEAGE_DLQ_TOPIC, value: "dlq.lineage.events"'),
    # Per-app DLQ subject, never a shared one — §2.11's two-apps-counting-each-other's-parks defect.
    # Rendered by the configmap under the resiliency gate, because the app ships the setting empty:
    # dead-lettering without a retry policy behind it parks on the first failure.
    ("chart/templates/configmap.yaml", 'RASK_NOTIFICATIONS_DLQ_TOPIC: "dlq.notifications"'),
]


@pytest.mark.parametrize(("relpath", "needle"), _PINNED_TOPICS, ids=[n for _, n in _PINNED_TOPICS])
def test_event_topic_constants_are_pinned(relpath: str, needle: str) -> None:
    """DATA-CONTRACT §7.2 names these exact topics; this pin keeps the doc and the code from drifting."""
    assert needle in (REPO / relpath).read_text(), (
        f"{relpath} no longer contains `{needle}` — the event-fabric topic contract (DATA-CONTRACT §7.2) "
        "names this exact constant. A deliberate rename must update the doc + this pin together; a "
        "BREAKING payload change must instead add a NEW .vN topic with parallel consumers."
    )


def test_medallion_never_re_types_the_catalog_control_topic() -> None:
    """The pin above says the constant is imported; this says no SECOND spelling of it may reappear.

    A pin is satisfiable by an import that some other module then shadows with its own literal — which
    is exactly how `core/config.py` came to carry `"catalog.control.v1"` while three other consumers
    imported `CONTROL_TOPIC`. The name belongs to its producer's model module (DATA-CONTRACT §7.2:
    "the ONE shared constant both sides import"); a rename there must reach every subscriber, and a
    duplicate is a subscriber the rename silently leaves listening to a topic nobody publishes to.
    """
    import ast  # local: the AST is the point — a prose mention of the topic in a comment is not a duplicate

    offenders = [
        f"{py.relative_to(REPO)}:{node.lineno}"
        for py in _svc("medallion").rglob("*.py")
        for node in ast.walk(ast.parse(py.read_text()))
        if isinstance(node, ast.Constant) and node.value == "catalog.control.v1"
    ]
    assert not offenders, (
        f"{offenders} spell `catalog.control.v1` out as a string literal instead of importing CONTROL_TOPIC "
        "from service_kit.control_events — the topic name has exactly one definition site (DATA-CONTRACT §7.2)."
    )


#: Both publish guards scan these roots. `services/` alone left the SHARED plane unguarded — see
#: `test_the_publish_guards_scan_the_PACKAGES_plane_too`.
_PUBLISH_SCAN_ROOTS: Final = (SERVICES, REPO / "packages")


def _publish_scan_files() -> list[Path]:
    """Every first-party Python file the publish guards below inspect."""
    return [py for root in _PUBLISH_SCAN_ROOTS for py in root.rglob("*.py")]


def _inline_topic_publishes() -> list[str]:
    """Every `dapr_publish.publish_event(...)` call site whose `topic_name` is an (f-)string literal —
    or that has no `topic_name` kwarg in view at all — instead of a named settings field / constant.

    An inline literal is a topic name CI cannot see: it bypasses the pins above, the chart's env
    retargeting, and the versioning rule (DATA-CONTRACT §7.2). Every real site today passes
    `topic_name=settings.<x>` / `self._topic` / a plumbed-through parameter — this keeps it that way.
    """
    offenders: list[str] = []
    literal_re = re.compile(r"topic_name\s*=\s*f?[\"']")
    for py in _publish_scan_files():
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            if "dapr_publish.publish_event(" not in line:
                continue
            window = "\n".join(lines[i : i + 8])
            if literal_re.search(window) or "topic_name=" not in window:
                offenders.append(f"{py.relative_to(REPO)}:{i + 1}")
    return offenders


def test_every_publish_site_uses_a_named_topic_constant() -> None:
    offenders = _inline_topic_publishes()
    assert not offenders, (
        "these publish sites pass an inline topic string (or no topic_name kwarg) instead of a named "
        f"constant/settings field: {offenders}. Inline topics dodge the pinned-constant contract "
        "(DATA-CONTRACT §7.2) — route the name through config or a shared constant."
    )


def _direct_publish_event_calls() -> list[str]:
    """Every ``.publish_event(`` call site OUTSIDE the wrapper module ``service_kit/dapr_publish.py``.

    The wrapper exists because the Dapr SDK's ``publish_event`` has no per-call timeout and no default
    gRPC deadline — a wedged sidecar hangs the caller forever. ``dapr_publish.publish_event(...)`` is
    the wrapper itself (excluded by the lookbehind); a direct ``client.publish_event(...)`` reopens the
    hang the wrapper closes, so no first-party module outside the wrapper may make one.
    """
    wrapper = SERVICE_KIT / "dapr_publish.py"
    direct_re = re.compile(r"(?<!dapr_publish)\.publish_event\(")
    offenders: list[str] = []
    for py in _publish_scan_files():
        if py == wrapper:
            continue
        for i, line in enumerate(py.read_text().splitlines()):
            if direct_re.search(line):
                offenders.append(f"{py.relative_to(REPO)}:{i + 1}")
    return offenders


def test_the_publish_guards_scan_the_PACKAGES_plane_too() -> None:
    """Both publish guards iterated `services/` only, so the shared plane was unguarded.

    Two publish sites live under `packages/` (`lakehouse/control_emit.py`, `lakehouse/outbox.py`). They are
    compliant today — which is exactly why nothing noticed the hole: a direct `client.publish_event(` or an
    inline `topic_name="literal"` added under `packages/` (or a new shared emitter) would ship green,
    reopening the unbounded-SDK-call hang the wrapper exists to close.

    Asserted on the SCANNED SET rather than on the offender list, because an offender list is empty in both
    the guarded and the unguarded case — that is the shape that let this survive.
    """
    scanned = {str(p.relative_to(REPO)) for p in _publish_scan_files()}

    for expected in ("packages/service-kit/src/service_kit/control_emit.py", "packages/service-kit/src/service_kit/lakehouse/outbox.py"):
        assert expected in scanned, f"the publish guards never look at {expected} — the packages plane is unguarded. Scanned {len(scanned)} files."


def test_every_publish_goes_through_the_timeout_wrapper() -> None:
    offenders = _direct_publish_event_calls()
    assert not offenders, (
        "these sites call .publish_event( directly instead of service_kit.dapr_publish.publish_event — the "
        f"unbounded SDK call a wedged sidecar hangs forever: {offenders}. Route the publish through the "
        "wrapper (it forwards **kwargs and enforces timeout_seconds)."
    )


#: Outcomes in ``authenticate`` that legitimately emit no audit line, by the reason they are exempt.
#: An entry here is a claim someone has to justify; a raw count is not.
_UNAUDITED_AUTHN_OUTCOMES = {
    "returns None because OIDC is disabled — there is no authentication event to record",
}


def _unaudited_outcomes(func_src: str, name: str) -> list[int]:
    """Line numbers of every ``raise``/``return`` in ``name`` with no ``audit(`` earlier in its block.

    STRUCTURAL, not a count, and not a line window. The gate this replaced was
    ``src.count("audit(") >= 2`` over the whole of security.py — a file that holds TEN audit calls, so
    EIGHT could be deleted (including the SUCCESS audit on the service-credential door) while it stayed
    green. A floor cannot know what it is missing; it can only know it has not gone to zero.

    "Earlier in its own block" is what the real code looks like: nine of the ten audits sit on the line
    directly above their outcome, and the tenth (the service-door SUCCESS) is separated from its
    ``return`` by an eleven-line comment. A proximity window would have to be tuned to that comment and
    would rot the moment someone edited it — the same bounded-window failure this audit found in
    nav-truth and transport-contract. Preceding-sibling-in-the-same-block has nothing to tune.
    """
    tree = ast.parse(func_src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name)

    def audits_in(node: ast.AST) -> bool:
        return any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "audit" for n in ast.walk(node))

    unaudited: list[int] = []

    def walk_block(body: list[ast.stmt], audited_above: bool) -> None:
        seen = audited_above
        for stmt in body:
            if isinstance(stmt, ast.Raise | ast.Return) and not (seen or audits_in(stmt)):
                unaudited.append(stmt.lineno)
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, field, None)
                if isinstance(inner, list):
                    walk_block(inner, seen)
            for handler in getattr(stmt, "handlers", []):
                walk_block(handler.body, seen)
            # Only an UNCONDITIONAL audit marks the rest of this block as covered. The first version
            # used `audits_in(stmt)`, which walks a whole compound statement — so an `if` whose body
            # audits and then RAISES marked every later outcome as audited, although that audit never
            # runs on the path that reaches them. Deleting the service-door SUCCESS audit still passed.
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and getattr(stmt.value.func, "id", "") == "audit":
                seen = True

    walk_block(fn.body, False)
    return unaudited


def test_authentication_outcomes_are_audited() -> None:
    """Compliance invariant (#41): EVERY outcome of ``authenticate`` is audited, not merely two of them.

    authn was entirely unlogged before #41, so brute-force and forged-token attempts were invisible.
    The point of the invariant is that no decision falls off the trail — which a count cannot express.
    """
    src = (_svc("catalog") / "api" / "security.py").read_text()
    unaudited = _unaudited_outcomes(src, "authenticate")
    assert len(unaudited) == len(_UNAUDITED_AUTHN_OUTCOMES), (
        f"authenticate() has {len(unaudited)} outcome(s) with no audit call in their own block "
        f"(lines {unaudited}), but only {len(_UNAUDITED_AUTHN_OUTCOMES)} are documented as exempt: "
        f"{sorted(_UNAUDITED_AUTHN_OUTCOMES)}. Either audit the new path or justify it here."
    )
    assert "SUCCESS" in src and "FAILURE" in src


def test_user_state_store_default_matches_the_component_the_catalog_is_scoped_to() -> None:
    """The `/v1/user-state/*` routes work only if THREE facts agree, and none of them is in the code.

    The catalog's `user_state_store` default names a Dapr component; that component must exist; and the
    catalog app-id must be in its `scopes` — an unscoped app-id gets "component not found" from the
    sidecar and every user's saved work 503s. All three live in `chart/values.yaml`, which is not edited
    when someone renames a component or trims a scope list, so nothing else would notice. This renders the
    chart and checks the agreement.
    """
    from catalog.core.config import Settings

    default = Settings.model_fields["user_state_store"].default
    rendered = _helm_template()
    component = next(
        (
            doc
            for doc in rendered.split("\n---")
            if re.search(r"^kind: Component$", doc, re.MULTILINE) and re.search(rf"^  name: {re.escape(default)}$", doc, re.MULTILINE)
        ),
        None,
    )
    assert component is not None, (
        f"the catalog defaults LANCE_USER_STATE_STORE to {default!r}, but the chart renders no Dapr "
        "Component by that name — every /v1/user-state call would 503"
    )
    assert re.search(r"type: state\.", component), f"{default} is not a state store"
    scopes = component.split("scopes:", 1)
    assert len(scopes) == 2 and re.search(r"^\s+- catalog$", scopes[1], re.MULTILINE), (
        f"the catalog app-id is not in {default}'s scopes — the sidecar refuses to load the component "
        "for it, so per-subject user state is unreachable however correct the code is"
    )


# --------------------------------------------------------------------------------------------------
# 8. The 2026-07-26 mistake classes, as mechanical guards rather than resolutions
# --------------------------------------------------------------------------------------------------


def _uncommented(text: str) -> str:
    """Blank out Helm/YAML comments, keeping line numbers, so prose ABOUT a pattern is not a use of it."""
    text = re.sub(r"\{\{-?/\*.*?\*/-?\}\}", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.DOTALL)
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())


def test_no_numeric_helm_default_can_swallow_an_explicit_zero() -> None:
    """`| default 255` rendered 255 for an explicit `0`, so a config change looked applied and nothing moved.

    Helm's `default` fires on any EMPTY value, and Go templates count `0` as empty. So every numeric knob
    written as `{{ .Values.x | default N }}` silently ignores the one value an operator is most likely to
    mean deliberately: `0` for "disabled", "unbounded", or "scaled to nothing". This cost a live debugging
    round on `frontend.idleTimeoutSeconds`, where `0` means "no connection lifetime cap" — exactly the
    value the live-stream fix needed — and where the rendered manifest kept saying 255.

    The correct idiom is `(hasKey $parent "key") | ternary $parent.key N`, which tests PRESENCE. This
    forbids the broken one chart-wide, because carving out exceptions is how the next instance gets in.
    """
    offenders = [
        f"{path.relative_to(REPO)}:{n}: {line.strip()}"
        for path in sorted((CHART / "templates").rglob("*"))
        if path.is_file()
        for n, line in enumerate(_uncommented(path.read_text()).splitlines(), 1)
        if re.search(r"\|\s*default\s+-?\d", line)
    ]
    assert not offenders, (
        "`| default <number>` treats an explicit 0 as absent, so the operator's value is silently "
        'replaced by the fallback. Use `(hasKey $parent "key") | ternary $parent.key <n>`:\n  ' + "\n  ".join(offenders)
    )


def test_no_pod_container_is_read_by_index() -> None:
    """`containerStatuses[0]` is the DAPR SIDECAR on a 2/2 pod, and its digest is identical everywhere.

    Reading index 0 to check whether a redeploy landed reported three services as unchanged when all three
    had in fact been replaced — the sidecar digest never moves, which should itself have been the tell.
    Any container read must name the container, e.g. `containerStatuses[?(@.name=="ray-head")]`.
    """
    roots = [REPO / "scripts", REPO / "Makefile", REPO / "tests", CHART]
    offenders = [
        f"{path.relative_to(REPO)}:{n}: {line.strip()}"
        for root in roots
        for path in ([root] if root.is_file() else sorted(root.rglob("*")))
        if path.is_file()
        and path.suffix in {"", ".sh", ".mjs", ".py", ".yaml", ".ts"}
        and path != Path(__file__)  # this file names the pattern in order to forbid it
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1)
        if "containerStatuses[0]" in line
    ]
    assert not offenders, (
        "a pod's containers are ordered by the injector, not by importance — index 0 is daprd on every "
        "sidecar-injected pod. Select by name instead:\n  " + "\n  ".join(offenders)
    )


def test_every_dapr_component_resolves_its_secrets_through_the_secret_store() -> None:
    """A password-bearing DSN was put in a k8s Secret — the exact anti-pattern `service_kit/governed/secrets.py` records.

    The estate's rule is that app-tier secrets come from OpenBao through the Dapr secret store as the sole
    source, fail-closed; k8s Secrets are for the infra tier (an owner's own credential, consumed by
    `secretKeyRef` in a pod spec). A Dapr Component is app tier. So a Component may reference a secret, but
    only by `secretKeyRef` WITH an `auth.secretStore` naming who resolves it — without that line Dapr looks
    in Kubernetes Secrets, which is how the anti-pattern comes back in while looking correct.

    It also forbids a literal credential in component metadata, which no `auth` block can redeem.
    """
    rendered = _helm_template()
    components = [d for d in rendered.split("\n---") if re.search(r"^kind: Component$", d, re.MULTILINE)]
    assert components, "the chart rendered no Dapr components — this guard would pass vacuously"

    for doc in components:
        name = re.search(r"^  name: (\S+)", doc, re.MULTILINE)
        label = name.group(1) if name else "<unnamed>"
        if "secretKeyRef" in doc:
            assert re.search(r"^auth:\s*$", doc, re.MULTILINE) and re.search(r"^\s+secretStore: \S", doc, re.MULTILINE), (
                f"component {label} uses secretKeyRef with no auth.secretStore, so Dapr resolves it from a Kubernetes Secret instead of OpenBao"
            )
        literal = re.search(r"value: \"?[a-z]+://[^\"\s]*:[^\"\s@]+@", doc)
        assert not literal, (
            f"component {label} carries a credential inline: {literal.group(0)[:60]}… — it must be a secretKeyRef resolved through the secret store"
        )


# --------------------------------------------------------------------------------------------------
# 9. The live-proof 2026-07-28 defect classes (docs/architecture/live-proof-2026-07-28.md)
#
# A first real end-to-end install on a stock kind cluster needed SIX manual overrides and one
# `kubectl set env` before it would come up. Every one of them was a chart default that only works on
# the maintainer's k3s box, and every failure mode was SILENT (Pending PVCs, ContainerCreating
# DaemonSets, a RayService stuck Initializing). Prose in a values comment cannot catch those; a render
# can. These guards are what make the fixes stay fixed.
# --------------------------------------------------------------------------------------------------


def _helm_notes(*set_values: str) -> str:
    """The rendered NOTES.txt — `helm template` deliberately omits it, and `helm install --dry-run`
    needs a live cluster (and trips over CRD ownership from an existing release), so neither can guard
    what the release notes actually SAY.

    A NOTES warning is the only thing some operators ever read, so its conditions deserve a real render
    rather than a grep over the template source. This builds a throwaway probe chart — the real
    values.yaml, the real _helpers.tpl, and the real NOTES.txt wrapped in a define + emitted as a
    ConfigMap value — so helm's own engine evaluates the same conditions with the same values.
    """
    import shutil
    import subprocess
    import tempfile

    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "notes-probe"
        (probe / "templates").mkdir(parents=True)
        (probe / "Chart.yaml").write_text("apiVersion: v2\nname: rask\nversion: 0.0.0\n")
        shutil.copy(CHART / "values.yaml", probe / "values.yaml")
        shutil.copy(CHART / "templates" / "_helpers.tpl", probe / "templates" / "_helpers.tpl")
        body = (CHART / "templates" / "NOTES.txt").read_text()
        (probe / "templates" / "notes-probe.yaml").write_text(
            '{{- define "notes.body" -}}\n'
            + body
            + "\n{{- end -}}\napiVersion: v1\nkind: ConfigMap\nmetadata: { name: notes-probe }\n"
            + 'data:\n  notes: {{ include "notes.body" . | quote }}\n'
        )
        argv = [helm, "template", "rask", str(probe)]
        # Side-loaded images: see `rask.image` in _helpers.tpl — the chart refuses a bare
        # `<component>:<tag>` unless this is set, because that is docker.io and not a local image.
        argv += ["--set", "image.localImages=true"]
        for value in set_values:
            argv += ["--set", value]
        out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
    # The notes ride as one quoted scalar; unescape so assertions read naturally.
    return out.encode().decode("unicode_escape")


def _pvc_docs(rendered: str) -> list[str]:
    return [d for d in rendered.split("\n---") if re.search(r"^kind: PersistentVolumeClaim$", d, re.MULTILINE)]


def test_no_pvc_hardcodes_a_provisioner_specific_storage_class() -> None:
    """`storageClassName: local-path` is k3s's provisioner NAME, and it does not exist anywhere else.

    kind's default class is `standard`; a managed cluster's is `gp3`/`standard-rwo`/whatever. A PVC that
    names a class the cluster does not have sits **Pending forever** — no pod event, no chart complaint,
    and (for the RustFS Tenant and the Ray HF cache) it takes the whole data plane or the Ray head down
    with it. That is exactly how live-proof defect 2 burned an install.

    The portable answer is to OMIT `storageClassName` so the cluster's DEFAULT class provisions, which is
    why the chart reads its class from a value that defaults to `""` and renders the key only `with` a
    non-empty one. This asserts on the RENDER: no PVC the chart emits may carry a hardcoded class, and no
    template may name one as a literal. (Note `storageClassName: ""` is NOT the same as omitting it — the
    empty string DISABLES dynamic provisioning — so an empty literal is an offender too.)
    """
    rendered = _helm_template("singleTenant.enabled=true", "explorer.enabled=true", "explorer.corpus.mode=pvc")
    assert _pvc_docs(rendered), "the chart rendered no PVCs — this guard would pass vacuously"
    offenders: list[str] = []
    # EVERY doc, not just kind: PersistentVolumeClaim — the four RustFS Tenant volumes are a
    # `volumeClaimTemplate` INSIDE the Tenant CR, which is precisely where defect 2 lived, and a
    # PVC-kind-only scan would have declared the fix verified while missing it. CRDs are skipped:
    # their OpenAPI schemas describe `storageClassName` as documentation, not as a value.
    for doc in rendered.split("\n---"):
        if re.search(r"^kind: CustomResourceDefinition$", doc, re.MULTILINE):
            continue
        name = re.search(r"^  name: (\S+)", doc, re.MULTILINE)
        kind = re.search(r"^kind: (\S+)", doc, re.MULTILINE)
        for cls in re.finditer(r"^\s+storageClassName:\s*(\S.*)$", doc, re.MULTILINE):
            offenders.append(f"{kind.group(1) if kind else '?'}/{name.group(1) if name else '?'} pins storageClassName: {cls.group(1)}")
    assert not offenders, (
        "these rendered PVCs pin a StorageClass with the chart's DEFAULT values, so the install only works "
        "on a cluster that happens to have that class:\n  " + "\n  ".join(offenders)
    )

    literals = [
        f"{path.relative_to(REPO)}:{n}: {line.strip()}"
        for path in sorted((CHART / "templates").rglob("*"))
        if path.is_file()
        for n, line in enumerate(_uncommented(path.read_text()).splitlines(), 1)
        if re.search(r"storageClassName:\s*[\"']?[a-zA-Z]", line)
    ]
    assert not literals, "a StorageClass name must come from values (empty => the cluster default), never a template literal:\n  " + "\n  ".join(literals)


def test_a_gpuless_estate_renders_a_gpuless_ray_serve() -> None:
    """ray.gpuCount=0 must make the htrflow Serve actor ask for 0 GPU — or nothing ever becomes ready.

    live-proof defect 3: `config.RASK_SERVE_GPU_FRAC` defaulted to "1.0" INDEPENDENTLY of ray.gpuCount, so
    a GPU-less cluster deployed a Serve deployment waiting on a resource that would never be advertised.
    The RayService then stayed `Initializing` forever, KubeRay never created the stable
    `<release>-ray-head-svc`, and the compute zone reported "Ray offline" beside a Running, healthy head —
    three symptoms, none of them pointing at the actual cause.

    Both render sites are checked, because a fraction that is right in the RayService and wrong in the
    fleet ConfigMap is the next debugging round.
    """
    rendered = _helm_template("singleTenant.enabled=true")  # ray.gpuCount defaults to 0
    fracs = re.findall(r"RASK_SERVE_GPU_FRAC:\s*\"?([0-9.]+)\"?", rendered)
    assert fracs, "RASK_SERVE_GPU_FRAC renders nowhere — the guard would pass vacuously"
    assert all(float(f) == 0.0 for f in fracs), (
        f"with ray.gpuCount=0 every RASK_SERVE_GPU_FRAC must derive to 0, got {fracs} — a Serve actor that "
        "reserves a GPU on a GPU-less cluster leaves the RayService Initializing forever"
    )
    rayservice = next((d for d in rendered.split("\n---") if re.search(r"^kind: RayService$", d, re.MULTILINE)), None)
    assert rayservice is not None, "singleTenant.enabled=true rendered no RayService"
    assert "nvidia.com/gpu" not in rayservice, "a GPU-less head must not request the nvidia.com/gpu extended resource"
    assert "runtimeClassName" not in rayservice, (
        "a GPU-less head must not name the nvidia RuntimeClass — its handler is not registered on a CPU node "
        "(and runtimeclass.yaml does not even create the object), so every head pod fails to create"
    )

    # ...and the GPU posture still renders the GPU wiring, or the derivation is a one-way ratchet.
    gpu = _helm_template("singleTenant.enabled=true", "ray.gpuCount=1", "nvdp.enabled=true")
    gpu_service = next((d for d in gpu.split("\n---") if re.search(r"^kind: RayService$", d, re.MULTILINE)), None)
    assert gpu_service is not None
    assert re.search(r"RASK_SERVE_GPU_FRAC:\s*\"1.0\"", gpu_service), "ray.gpuCount=1 must pass config.RASK_SERVE_GPU_FRAC through"
    assert "nvidia.com/gpu: 1" in gpu_service and "runtimeClassName: nvidia" in gpu_service
    assert re.search(r"^kind: RuntimeClass$", gpu, re.MULTILINE), "a GPU estate must create the nvidia RuntimeClass"


def test_the_gpu_device_plugin_cannot_render_without_gpu_workloads() -> None:
    """nvdp + GPU-Feature-Discovery on a GPU-less cluster = two DaemonSets ContainerCreating forever.

    live-proof defect 6. Helm resolves a subchart `condition:` against a STATIC values path and cannot
    express `ray.gpuCount > 0`, so the chart cannot gate the device plugin on the same expression it gates
    every other GPU render on. It fails the render on the incoherent pair instead — which is a gate, just
    one that speaks. The reverse pair is legitimate (an externally-managed device plugin) and only warns.
    """
    import subprocess

    with pytest.raises(subprocess.CalledProcessError) as exc:
        _helm_template("nvdp.enabled=true")  # ray.gpuCount defaults to 0
    message = (exc.value.stderr or "") + (exc.value.stdout or "")
    assert "nvdp.enabled=true" in message and "ray.gpuCount" in message, f"the render must fail with the incoherent pair NAMED, got: {message[:400]}"
    assert "--set nvdp.enabled=false" in message, "a fail-closed guard must carry the fix in the message"

    # The coherent GPU pair renders, and it renders the plugin.
    gpu = _helm_template("ray.gpuCount=1", "nvdp.enabled=true")
    assert "nvdp" in gpu, "the coherent GPU pair must actually install the device plugin"
    # The default (GPU-less) render must carry neither DaemonSet.
    plain = _helm_template()
    daemonsets = [d for d in plain.split("\n---") if re.search(r"^kind: DaemonSet$", d, re.MULTILINE)]
    gpu_daemons = [d for d in daemonsets if "nvdp" in d or "gpu-feature-discovery" in d]
    assert not gpu_daemons, (
        f"the DEFAULT (GPU-less) render still emits {len(gpu_daemons)} GPU DaemonSet(s) — they will sit "
        "ContainerCreating forever on any cluster without an nvidia OCI runtime"
    )


def test_no_workload_mounts_a_hostpath_that_must_pre_exist() -> None:
    """`hostPath` + `type: Directory` = ContainerCreating forever unless a human prepared the node.

    live-proof defect 5: `explorer.enabled=true` mounted /var/media-corpus with `type: Directory`, so on a
    fresh cluster all three media pods wedged with a kubelet event as the only clue — and the plan already
    said no hostPath ships. First-party workloads must default to a volume that needs no node preparation
    (emptyDir for dev, a PVC for prod); where a node-local path IS the point (the OTel collector tailing
    /var/log/pods) it must be `DirectoryOrCreate`, never the fail-if-absent `Directory`.
    """
    rendered = _helm_template("singleTenant.enabled=true", "explorer.enabled=true", "observability.enabled=true")
    offenders: list[str] = []
    for doc in rendered.split("\n---"):
        if "hostPath" not in doc:
            continue
        name = re.search(r"^  name: (\S+)", doc, re.MULTILINE)
        for m in re.finditer(r"hostPath:\s*\{?[^}\n]*", doc):
            if re.search(r"type:\s*Directory\b(?!OrCreate)", m.group(0)):
                offenders.append(f"{name.group(1) if name else '?'}: {m.group(0).strip()[:90]}")
    assert not offenders, (
        "these rendered workloads mount a hostPath that must ALREADY exist on the node, so a fresh cluster "
        "wedges them in ContainerCreating:\n  " + "\n  ".join(offenders)
    )
    # The media corpus specifically: the DEFAULT must be node-independent.
    media_pods = [d for d in rendered.split("\n---") if "app.kubernetes.io/component: viewer" in d and "kind: Deployment" in d]
    assert media_pods, "explorer.enabled=true rendered no viewer Deployment"
    assert "hostPath" not in media_pods[0], (
        "the media corpus volume still defaults to a hostPath — set explorer.corpus.mode's default to a volume that works on an unprepared node"
    )


def test_the_lineage_durability_chain_is_on_by_default() -> None:
    """The #4 chain (stage -> publish -> drain) shipped OFF while NOTES warned about it on every install.

    live-proof defect 7. A default that the release notes tell every operator to change is the wrong
    default; worse, the warning became scenery. Both halves are ON now — and the pair matters: reconcile
    without the outbox can only back-fill that a version exists (author/inputs/columnLineage are gone),
    and the outbox without reconcile stages events nothing drains. This pins BOTH, and pins that the NOTES
    warning still fires (loudly) when someone turns one off.
    """
    import yaml

    values = yaml.safe_load((CHART / "values.yaml").read_text())
    lineage = values["services"]["lineage"]
    assert lineage["reconcile"]["enabled"] is True, "services.lineage.reconcile must default ON (it drains the outbox)"
    assert lineage["outbox"]["enabled"] is True, "services.lineage.outbox must default ON (reconcile has nothing to drain without it)"

    assert "DATA LOSS WINDOW OPEN" not in _helm_notes(), "the default install must not print a durability warning it cannot act on"
    broken = _helm_notes("services.lineage.reconcile.enabled=false")
    assert "DATA LOSS WINDOW OPEN" in broken and "reconcile.enabled=false" in broken, (
        f"disabling reconcile must produce an unmissable NOTES warning naming the half that is missing; got:\n{broken}"
    )
    half = _helm_notes("services.lineage.outbox.enabled=false")
    assert "outbox.enabled=false" in half and "PERMANENTLY" in half, (
        f"disabling only the outbox must say what is lost (author/inputs/columnLineage), got:\n{half}"
    )


# --------------------------------------------------------------------------------------------------
# 10. The 2026-07-28 install-flow defects (live-proof "Install-flow notes" + defects 1 and 3)
# --------------------------------------------------------------------------------------------------


def _docs(rendered: str) -> list[str]:
    return rendered.split("\n---")


def _job_by_component(rendered: str, component: str) -> str | None:
    """The rendered Job carrying `app.kubernetes.io/component: <component>`.

    Selected by LABEL, not by name: `_helm_template` renders without a release name (helm's
    `release-name` placeholder), and the bootstrap Jobs now carry a release-revision suffix — a
    name-prefix match would encode both accidents.
    """
    for doc in _docs(rendered):
        if not re.search(r"^kind: Job$", doc, re.MULTILINE):
            continue
        if re.search(rf"^\s+app\.kubernetes\.io/component: {re.escape(component)}$", doc, re.MULTILINE):
            return doc
    return None


#: The bootstrap Jobs that other release resources must wait for IN ORDER TO BECOME READY: the OpenFGA
#: schema migration (the server crash-loops against an unmigrated datastore), the OpenBao seed (the
#: medallion movers resolve S3 creds through the Dapr secret store at boot), the JetStream provisioner
#: (a daprd sidecar subscribes at startup) and the bucket-init (the lakehouse apps' object store).
_BOOTSTRAP_JOBS = ["openfga-migrate", "openbao-seed", "nats-stream", "rustfs-mkbucket"]

#: The inverse set — Jobs that wait for the APPS and that nothing waits on. A post-install hook is
#: exactly the right shape for these, and converting them would be the same mistake mirrored.
_POST_APP_HOOKS = ["greptimedb-ttl", "kueue-setup"]


def test_no_job_that_gates_readiness_is_a_post_install_hook() -> None:
    """`helm install --wait` must be safe on a fresh cluster, with no wrapper script.

    live-proof defect 1. helm's order is: pre-install hooks -> apply manifests -> (--wait) block until
    every resource is Ready -> post-install hooks. The OpenFGA schema migration was a POST-install hook
    and the OpenFGA server cannot start against an unmigrated datastore, so `--wait` blocked on the
    server, the server blocked on the migration, and the migration was queued behind `--wait`. Revs 1
    and 2 of the live-proof install died on "context deadline exceeded"; the OpenBao seed (also a
    post-install hook) never fired at all; and `scripts/e2e_stack.sh` exists in part to drop `--wait`
    and re-sequence the whole thing by hand.

    Moving those hooks EARLIER cannot fix it either: a `pre-install` hook runs before ANY release
    manifest exists, so the migrate Job would wait for an AGE Postgres that has not been created yet —
    the same deadlock one phase to the left, now blocking every other resource too. The fix is to take
    them out of the hook lifecycle entirely, which is what this pins.
    """
    rendered = _helm_template("singleTenant.enabled=true", "explorer.enabled=true")
    offenders: list[str] = []
    for comp in _BOOTSTRAP_JOBS:
        doc = _job_by_component(rendered, comp)
        assert doc is not None, f"{comp} renders no Job with the default values — this guard would pass vacuously"
        if re.search(r'^\s*"?helm\.sh/hook"?:', doc, re.MULTILINE):
            offenders.append(comp)
    assert not offenders, (
        "these Jobs gate another resource's READINESS, so as helm hooks they deadlock `helm install --wait`: "
        + ", ".join(offenders)
        + " — apply them in the ordinary wave (see 'BOOTSTRAP JOBS' in chart/templates/_helpers.tpl)"
    )

    # A plain Job's spec is immutable, so the name must change per revision or `helm upgrade` fails with
    # "field is immutable" the first time anything about the Job changes.
    for comp in _BOOTSTRAP_JOBS:
        doc = _job_by_component(rendered, comp)
        assert doc is not None
        name = re.search(r"^  name: (\S+)", doc, re.MULTILINE)
        assert name is not None and re.search(r"-r\d+$", name.group(1)), (
            f"{comp} must carry the release revision in its name (a plain Job's spec is immutable), got {name and name.group(1)}"
        )

    # The mirror image: a Job that waits for the APPS must NOT be pulled into the wave, or `--wait`
    # starts blocking on something that by definition cannot finish until the wait is over.
    for comp in _POST_APP_HOOKS:
        doc = _job_by_component(rendered, comp)
        assert doc is not None, f"{comp} renders no Job — this half of the guard would pass vacuously"
        assert re.search(r'^\s*"?helm\.sh/hook"?:\s*post-install', doc, re.MULTILINE), (
            f"{comp} waits for the apps and nothing waits on it — it belongs in post-install, not the apply wave"
        )


def test_the_bucket_init_verifies_the_buckets_the_operator_owns() -> None:
    """A provisioner that never checks what it did NOT provision is how a data plane goes missing.

    live-proof defect 3: the rustfs-operator refused to reconcile the Tenant
    (StatefulSetUpdateValidationFailed, "Reconcile is blocked by user-fixable configuration"), so
    `spec.buckets` — the whole static platform set — was never created. The bucket-init Job went green
    because it only ever created its OWN values-driven buckets, every layer in between reported
    healthy, and the single symptom was an HTTP 500 in the storage browser three services away.

    So the Job must VERIFY the operator-owned set and fail loudly, with the diagnosis in the message.
    It must NOT create them: `mc mb` here would paper over a Tenant that is still broken for
    everything else it owns.
    """
    import yaml

    values = yaml.safe_load((CHART / "values.yaml").read_text())
    expected = values["rustfs"]["buckets"]
    assert expected, "rustfs.buckets is empty — this guard would pass vacuously"

    rendered = _helm_template("singleTenant.enabled=true", "explorer.enabled=true")
    job = _job_by_component(rendered, "rustfs-mkbucket")
    assert job is not None, "the bucket-init Job does not render"

    missing = [b for b in expected if b not in job]
    assert not missing, f"the bucket-init Job never mentions these operator-owned buckets, so it cannot notice they are absent: {missing}"

    assert "exit 1" in job, "the bucket-init Job must FAIL when the operator-owned buckets are absent, not log and pass"
    assert "kubectl describe tenant" in job, "the failure message must carry the command that shows WHY the Tenant did not reconcile"
    assert "rustfs.storageClass" in job, "the failure message must name the usual cause (a StorageClass the cluster does not have)"
    # The create/verify split: the operator's buckets must not be silently created behind its back.
    for bucket in expected:
        assert f"mc mb --ignore-existing rfs/{bucket}\n" not in job or bucket == values["rustfs"]["bucket"], (
            f"{bucket} is operator-owned (rustfs.buckets) — creating it here would mask a Tenant that never reconciled"
        )


def test_every_dapr_annotated_pod_carries_the_injector_webhook_label() -> None:
    """Fail-closed sidecar injection is only safe if the two markers stay in lockstep.

    The second install-time ordering defect of 2026-07-28: Dapr's injector is a mutating webhook
    shipping `failurePolicy: Ignore`, and the Dapr control plane is a SUBCHART of this release — so on
    a fresh cluster the app pods are created alongside the injector, the API server calls a webhook
    with no endpoints, and it SILENTLY admits them with no daprd container. Nothing recreates such a
    pod (a CrashLoopBackOff restarts the container inside the same pod), so every governed app dies
    forever on "secret unavailable from Dapr store … failing closed" and `helm install --wait` times
    out. `scripts/e2e_stack.sh` works around it by deleting and recreating the app pods by hand.

    The chart now sets `failurePolicy: Fail` with an `objectSelector` on the `dapr.io/enabled` LABEL.
    That makes the correspondence load-bearing in BOTH directions of failure:
      * annotation without label -> the webhook is never called -> silently un-injected again;
      * an unscoped `Fail` -> the injector's own pod cannot be admitted -> the cluster wedges.
    So: every pod template carrying the annotation must carry the label, and the webhook's selector
    must be exactly that label.
    """
    import yaml

    rendered = _helm_template("singleTenant.enabled=true", "explorer.enabled=true")
    docs = [d for d in yaml.safe_load_all(rendered) if d]

    webhook = next(
        (w for d in docs if d.get("kind") == "MutatingWebhookConfiguration" for w in (d.get("webhooks") or []) if w.get("name") == "sidecar-injector.dapr.io"),
        None,
    )
    assert webhook is not None, "the Dapr sidecar-injector webhook does not render — this guard would pass vacuously"
    assert webhook["failurePolicy"] == "Fail", (
        "the Dapr injector webhook must fail CLOSED: with `Ignore` a pod created before the injector is "
        "ready is admitted with no sidecar, and nothing ever fixes it"
    )
    selector = (webhook.get("objectSelector") or {}).get("matchLabels") or {}
    assert selector == {"dapr.io/enabled": "true"}, (
        f"a fail-closed POD webhook MUST be scoped by objectSelector or it blocks its own injector pod and wedges the cluster; got {selector!r}"
    )

    annotated = 0
    missing: list[str] = []
    for doc in docs:
        template = (doc.get("spec") or {}).get("template") if isinstance(doc.get("spec"), dict) else None
        if not isinstance(template, dict):
            continue
        meta = template.get("metadata") or {}
        if (meta.get("annotations") or {}).get("dapr.io/enabled") != "true":
            continue
        annotated += 1
        if (meta.get("labels") or {}).get("dapr.io/enabled") != "true":
            missing.append(f"{doc.get('kind')}/{(doc.get('metadata') or {}).get('name')}")
    assert annotated, "no pod template asks for a Dapr sidecar — this guard would pass vacuously"
    assert not missing, (
        "these pod templates want a sidecar (dapr.io/enabled ANNOTATION) but do not carry the matching "
        f"LABEL, so the fail-closed webhook skips them and they come up un-injected: {missing}"
    )


def test_every_pod_whose_app_fails_closed_on_the_app_token_is_given_one() -> None:
    """A pod running code that REFUSES TO START without APP_API_TOKEN must be rendered one.

    `service_kit.governed.dapr_auth.assert_app_token_configured` raises at startup when Dapr ingest is
    on and the token is unset — deliberately, because the alternative is a live sidecar-delivered route
    with no authentication. That makes the token a startup PRECONDITION for those apps, and a
    precondition the chart can omit silently: the render is valid YAML, `helm upgrade` succeeds, and the
    pod CrashLoopBackOffs with the reason buried in container logs.

    `notifications` shipped exactly that. It writes no Lance, and in fleet.yaml the token had only ever
    been reachable from inside `if $svc.lanceWriter` — an unrelated STORAGE flag — so the first fleet
    service to host a Dapr-delivered route rendered `RASK_DAPR_ENABLED=true` with no token at all. Every
    prior caller of the assert is a lance-plane pod templated somewhere else, each naming APP_API_TOKEN
    explicitly, which is why one shared render site had never been needed and its absence never showed.

    Derived from the module each Deployment actually RUNS, not from a hand-kept list of service names —
    a list is the thing that drifts, which is the lesson the lineage-allowlist gate already encodes.
    """
    rendered = _helm_template()

    fail_closed = {path.relative_to(SERVICES).parts[0] for path in SERVICES.rglob("*.py") if "assert_app_token_configured(" in path.read_text(errors="ignore")}
    assert fail_closed, "no service calls assert_app_token_configured — this guard has nothing to check"

    # fleet.yaml renders `args: - "<svc>:app"`; the lance templates render `<pkg>.<module>:app`. Both
    # name the import root, which is the services/ directory name.
    module_re = re.compile(r'^\s*-\s+"?([a-z_]+)(?:\.[a-z_.]+)?:app"?\s*$', re.MULTILINE)
    checked = 0
    missing: list[str] = []
    for doc in yaml.safe_load_all(rendered):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        raw = yaml.safe_dump(doc)
        if not {m.group(1) for m in module_re.finditer(raw)} & fail_closed:
            continue
        checked += 1
        if "APP_API_TOKEN" not in raw:
            missing.append((doc.get("metadata") or {}).get("name", "?"))

    assert checked, "no rendered Deployment runs a fail-closed module — the module parse has drifted"
    assert not missing, (
        f"{sorted(missing)} run code that calls assert_app_token_configured but are rendered WITHOUT "
        "APP_API_TOKEN. The chart installs cleanly and the pod CrashLoopBackOffs on startup. Give the "
        "service `daprIngest: true` in values.yaml (fleet.yaml) or render the token in its own template."
    )


def test_the_ingress_admits_a_real_page_image() -> None:
    """ingress-nginx caps a request body at **1 MB** by default, and every image surface in this
    estate is larger than that.

    Measured 2026-08-06 on the deployed stack: the inference playground refused a 2239 kB page image
    with `HTTP 413`. The zone's own guard allows 25 MB and the Ray Serve ingress never saw the bytes
    — the EDGE rejected them, so the failure read as "inference is broken" rather than "the ingress
    has a 1 MB cap". The same cap sits in front of the annotator's Arrow import and the medallion's
    multi-image batch posts.

    Asserted RENDERED and numerically, exactly like the read-timeout sibling above: an annotation
    that is merely present but smaller than a real page image is decoration.
    """
    rendered = _helm_template("ingress.enabled=true")
    ingress = next((doc for doc in rendered.split("\n---") if re.search(r"^kind: Ingress$", doc, re.MULTILINE)), None)
    assert ingress is not None, "ingress.enabled=true rendered no Ingress"
    m = re.search(r"nginx\.ingress\.kubernetes\.io/proxy-body-size:\s*\"?(\d+)([mMgG])\"?", ingress)
    assert m, "the Ingress carries no nginx.ingress.kubernetes.io/proxy-body-size — nginx's 1 MB default 413s every page image before it reaches a zone"
    megabytes = int(m.group(1)) * (1024 if m.group(2).lower() == "g" else 1)
    assert megabytes >= 25, f"proxy-body-size is {m.group(0)} — below the zones' own 25 MB inference guard, so the edge is the tighter limit"


def test_auth_is_ON_by_default_and_an_open_estate_must_be_ASKED_for() -> None:
    """A security default has to fail CLOSED.

    `auth.enabled` and `frontend.oidc.enabled` were both `false` in values.yaml, with
    values-prod.yaml flipping them — so every install that forgot `-f values-prod.yaml` came up
    UNGOVERNED, green, and indistinguishable from a governed one. Measured 2026-08-06: helm revision
    29 landed exactly that way, which also stopped rendering the session Secret and left
    `rask-web-models` unable to start a pod against a spec that still referenced it.

    Both must be ON in the base values, and they must move TOGETHER — the chart refuses the
    half-governed pair (templates/auth-consistency.yaml), so a default that enabled one and not the
    other would fail every render instead of protecting anything.
    """
    values = yaml.safe_load((REPO / "chart/values.yaml").read_text())
    assert values["auth"]["enabled"] is True, "auth defaults OFF — a forgotten values file silently un-governs the estate"
    assert values["frontend"]["oidc"]["enabled"] is True, "the UI's sign-in flow defaults OFF while auth defaults ON — the chart refuses that pair"


def test_the_chart_REFUSES_to_render_oidc_without_a_session_secret() -> None:
    """The other half of failing closed: enabling the flow is not enough if the cookie is unsealed.

    Deliberately asserted as a RENDER FAILURE rather than a defaulted value. Shipping a plausible
    session secret in values.yaml is how a dev key reaches production — the local loop supplies its
    own in the Makefile, in the open, where it is obviously a dev value.
    """
    # subprocess directly: `_helm_template` raises on a non-zero exit, and the non-zero exit IS the
    # assertion here.
    import shutil
    import subprocess

    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    result = subprocess.run(
        [helm, "template", "rask", str(REPO / "chart"), "--set", "image.localImages=true"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode != 0, "a bare render succeeded — it must refuse without a sessionSecret"
    assert "sessionSecret" in result.stderr, f"it refused for the wrong reason: {result.stderr[-300:]}"


def test_a_per_component_pin_beats_the_global_tag_and_a_digest_beats_the_pin() -> None:
    """#135 — the chart must be able to describe a fleet that is NOT one tag.

    One `image.tag` could not: a live estate ran a catalog tag across 11 services, a different tag on
    gateway/compute/controlplane, a third across the 7 zones, and an ingest DIGEST. Rendering all of
    that from one value is what made `helm upgrade` destructive — every image rewritten to a tag the
    node did not hold, whole fleet ImagePullBackOff, recovered by hand. Observed twice on 2026-08-06.

    Precedence is asserted end to end, because a pin that is merely ACCEPTED and then overridden is
    worse than no pin: it reads as safety while the deploy still rewrites the image.
    """
    pinned = _helm_template("image.localImages=false", "image.repository=reg.example", "image.tags.gateway=PINNED")
    assert 'image: "reg.example/gateway:PINNED"' in pinned, "a per-component tag did not reach the render"
    assert 'image: "reg.example/compute:dev"' in pinned, "the pin leaked onto a component that did not ask for it"

    # A digest is a CONTENT pin — a reconciler's, typically — so no tag may undo it.
    both = _helm_template("image.localImages=false", "image.repository=reg.example", "image.tags.gateway=IGNORED", "image.digests.gateway=sha256:abc")
    assert 'image: "reg.example/gateway@sha256:abc"' in both, "a per-component tag overrode a digest"


@pytest.mark.parametrize("ray_enabled", ["true", "false"])
def test_no_env_var_is_rendered_TWICE_on_any_workload(ray_enabled: str) -> None:
    """A duplicated env NAME is not a cosmetic defect — it makes the release unupgradable.

    Kubernetes' strategic-merge-patch keys the env list by `name`, so a duplicate makes the patch's
    element order disagree with `$setElementOrder` and helm refuses with
    `The order in patch list … doesn't match`. On 2026-08-06 `COMPUTE_SERVE_URL` was rendered twice
    for the compute zone — once ungated, once in the ray-enabled block — and the resulting upgrade
    aborted AFTER partially applying, taking ~20 deployments to ImagePullBackOff.

    Parametrized over ray because that is precisely the toggle that produced the collision: the pair
    only overlapped when ray was on, so a single-mode check would have passed while the estate that
    matters broke.
    """
    rendered = _helm_template(f"ray.enabled={ray_enabled}")
    offenders: list[str] = []
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") not in {"Deployment", "StatefulSet", "Job", "CronJob"}:
            continue
        spec = doc["spec"]["template"]["spec"] if doc["kind"] != "CronJob" else doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            names = [e["name"] for e in container.get("env", [])]
            dupes = {n for n in names if names.count(n) > 1}
            if dupes:
                offenders.append(f"{doc['metadata']['name']}/{container['name']}: {sorted(dupes)}")
    assert offenders == [], f"duplicated env names make the release unupgradable: {offenders}"


def test_the_INGEST_stream_has_ONE_definition_and_the_chart_agrees_with_the_code() -> None:
    """Two definitions of one stream, reconciled only by "whoever creates it first wins".

    `services/ingest/.../queue.py` declares `RetentionPolicy.WORK_QUEUE` and BUILDS ON it — "a message
    is REMOVED once acked … which is why this plane needs no side ledger" is the reasoning that keeps
    a per-unit ledger out of this estate. The chart created the same stream with `--retention limits`, and in-cluster
    the Job wins. Measured on the live stream 2026-08-06: retention=limits, deny_purge=true,
    deny_delete=true, max_age=168h — so acked messages were RETAINED and `messages` was not
    outstanding work. The design claim was false exactly where it shipped.

    Three assertions because there were three statements and two were false:
      1. the chart's `--retention` for INGEST matches the code's RetentionPolicy;
      2. the chart does not contradict ITSELF (it described the stream as WorkQueuePolicy while
         creating it as limits, twenty-seven lines apart);
      3. `--deny-purge` / `--deny-delete` are absent on the work-queue path — they protect a retained
         RECORD, which a work queue does not have, and their only measured effect was making the
         plane's own `release_run -> purge_stream` return an inert 0 while the server refused it.
    """
    job = (REPO / "chart/templates/nats-stream-job.yaml").read_text()
    queue = (REPO / "services/ingest/src/ingest/queue.py").read_text()

    code_is_workqueue = "RetentionPolicy.WORK_QUEUE" in queue
    assert code_is_workqueue, "queue.py stopped declaring WORK_QUEUE — update this gate WITH the design change, not after it"

    # (1) The creation call INGEST actually uses.
    ingest_call = next((ln for ln in job.splitlines() if "INGEST" in ln and "_if_missing" in ln), "")
    assert ingest_call, "no creation call for INGEST — it is created somewhere this gate cannot see"
    fn = ingest_call.strip().split()[0]
    body = job.split(f"{fn}() {{", 1)[1].split("\n              }", 1)[0]
    assert "--retention work" in body, f"INGEST is created by {fn}() with a retention the code does not declare: {body.strip()[:200]}"

    # (3) …and without the flags that only make sense for a retained log.
    for flag in ("--deny-purge", "--deny-delete"):
        assert flag not in body, f"{flag} on a work queue protects a record it does not have, and breaks purge-based reclamation"

    # (2) The chart must not describe the stream as something it does not create.
    if "WorkQueuePolicy" in job:
        assert "--retention work" in body, "the chart calls INGEST a WorkQueuePolicy stream while creating it otherwise"


def test_the_lineage_allowlist_is_DERIVED_from_every_declared_identity() -> None:
    """The door's allowlist and the callers' claims must come from ONE value, not two lists.

    `LINEAGE_SERVICE_SUBJECTS` was a hand-written string naming three services, and a list that has
    to be kept in sync with the set of services that emit lineage WILL drift — nothing connected the
    two. It drifted twice: the trainer in 2026-07, and `service-ingest` on 2026-08-06, where every
    ingest emit 403'd for a day while the data landed perfectly.

    The failure is SILENT by construction, which is why a gate is the only thing that catches it. The
    emitter swallows a refused emit on purpose — a landed commit must not become a failed run — so
    the symptom is an absence in the graph, not an error anywhere.

    A service now DECLARES itself by setting `env.RASK_LINEAGE_SERVICE_IDENTITY`, and the chart reads
    those declarations. This asserts the derivation actually holds: declare an identity and you are
    admitted; the string cannot be edited out from under a declaration.
    """
    import re

    rendered = _helm_template("auth.enabled=true")

    declared = {m.group(1) for m in re.finditer(r'name:\s*RASK_LINEAGE_SERVICE_IDENTITY,\s*value:\s*"?([\w-]+)"?', rendered)}
    assert declared, "no service declares a lineage identity — the derivation has nothing to read, so this gate is vacuous"

    allowlist_match = re.search(r'name:\s*LINEAGE_SERVICE_SUBJECTS,\s*value:\s*"([^"]*)"', rendered)
    assert allowlist_match, "the lineage door renders no allowlist at all — every service emit will 401"
    allowed = set(allowlist_match.group(1).split(","))

    missing = declared - allowed
    assert not missing, (
        f"{sorted(missing)} declare RASK_LINEAGE_SERVICE_IDENTITY but are NOT in LINEAGE_SERVICE_SUBJECTS. "
        "Their lineage emits will 403 and the emitter will swallow it — the data lands and the graph "
        "stays empty, with nothing reporting the gap."
    )


def test_the_allowlist_admits_NO_EMPTY_subject() -> None:
    """An unset identity must contribute nothing, not an empty string.

    `compact` is what provides this. Without it a service whose identity is unset renders a bare `,,`
    and the door's allowlist contains "" — which admits a caller presenting no identity at all, on a
    door whose entire purpose is to require one.
    """
    import re

    rendered = _helm_template("auth.enabled=true")
    match = re.search(r'name:\s*LINEAGE_SERVICE_SUBJECTS,\s*value:\s*"([^"]*)"', rendered)
    assert match

    subjects = match.group(1).split(",")

    assert "" not in subjects, f"the allowlist contains an EMPTY subject: {subjects!r} — it would admit an unidentified caller"
    assert len(subjects) == len(set(subjects)), f"the allowlist repeats a subject: {subjects!r}"


def test_every_DURABLE_pubsub_component_has_a_sidecar_retry_target() -> None:
    """A trigger consumer that must not lose messages must also be named in the Resiliency CRD.

    THE REGRESSION THIS GATES. The cascade's control component
    (`catalog-control-pubsub-<producer>`) carried a `durableName` — the chart's own marker for "a
    trigger published while this app is down must be DELIVERED on recovery" — and was absent from
    the Resiliency CRD's `targets.components`, which ranges only over the `lance.subPubsub` family.
    The `/publication-arrival` subscription declares a `dead_letter_topic`, and Dapr sends there
    after the FIRST failure when no retry policy targets the component. So the most expensive
    trigger in the estate got zero retries and parked on one transient blip — while the CRD existed,
    the app was in `scopes:`, and every neighbour was covered.

    The rule is stated as a PROPERTY rather than a list of names on purpose: a future subscriber
    added to one template and not the other fails here, which is exactly how this one was missed.
    """
    rendered = _helm_template("dapr.enabled=true", "dapr.resiliency.enabled=true")
    docs = [d for d in yaml.safe_load_all(rendered) if d]

    durable = {
        d["metadata"]["name"]
        for d in docs
        if d.get("kind") == "Component"
        and (d.get("spec") or {}).get("type") == "pubsub.jetstream"
        and any(m.get("name") == "durableName" for m in (d.get("spec") or {}).get("metadata") or [])
        # DLQ components are EXEMPT, and not as a convenience. A dead-letter handler must
        # unconditionally ACK — a RETRY from a DLQ route requeues the message onto the DLQ forever —
        # so its subscription never returns RETRY and an inbound retry policy could never engage.
        # Requiring one would push a meaningless target into the CRD and teach the next reader that
        # a DLQ is retried, which is the opposite of the rule. (This exemption is not hypothetical:
        # `lineage-pubsub-lineage-dlq` is durable, by design, and correctly has no target.)
        and not d["metadata"]["name"].endswith("-dlq")
    }
    assert durable, "no durable pub/sub component rendered — this gate would pass vacuously"

    targeted: set[str] = set()
    for d in docs:
        if d.get("kind") != "Resiliency":
            continue
        targeted |= set(((d.get("spec") or {}).get("targets") or {}).get("components") or {})

    missing = durable - targeted
    assert not missing, (
        f"durable pub/sub components with NO inbound retry target: {sorted(missing)} — with a dead_letter_topic declared, Dapr parks these on the FIRST failure"
    )


def test_the_rustfs_tenant_carries_NO_plaintext_credential() -> None:
    """The Tenant CR's OIDC client secret must be a `secretKeyRef`, never a `value:`.

    THE REGRESSION THIS GATES. `RUSTFS_IDENTITY_OPENID_CLIENT_SECRET` shipped as
    `value: {{ .Values.dex.clientSecret }}` — readable in `kubectl get tenant -o yaml`,
    `kubectl describe` and `helm get manifest` — while every sibling credential in the estate was
    already behind a guard. It sat outside that guard because a Tenant is consumed by the RustFS
    operator, which has no daprd sidecar and so cannot read the Dapr secret store the fleet services
    use. That is the case `infra-credentials.yaml` exists for, and this asserts the Tenant actually
    uses it.

    Latent-by-default is not a defence: `rustfs.oidc.enabled` is off in the shipped values, so this
    renders only on estates running STS credential vending — which is precisely where a leaked client
    secret is worth the most.
    """
    rendered = _helm_template("rustfs.enabled=true", "rustfs.oidc.enabled=true")
    docs = [d for d in yaml.safe_load_all(rendered) if d]

    tenants = [d for d in docs if d.get("kind") == "Tenant"]
    assert tenants, "no Tenant rendered — this gate would pass vacuously"

    for tenant in tenants:
        for env in (tenant.get("spec") or {}).get("env") or []:
            if not str(env.get("name", "")).endswith("_CLIENT_SECRET"):
                continue
            assert "value" not in env, f"{env['name']} renders a PLAINTEXT value on the Tenant CR: {env!r}"
            ref = ((env.get("valueFrom") or {}).get("secretKeyRef")) or {}
            assert ref.get("name") and ref.get("key"), f"{env['name']} has neither a value nor a usable secretKeyRef: {env!r}"

            # The reference must RESOLVE — a secretKeyRef at an absent key is a pod that never starts,
            # and it would only surface on the enabled path, which is the narrowest possible place to
            # discover it.
            secrets = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Secret"}
            target = secrets.get(ref["name"])
            assert target is not None, f"{env['name']} references Secret {ref['name']!r}, which the chart does not render"
            keys = set(target.get("stringData") or {}) | set(target.get("data") or {})
            assert ref["key"] in keys, f"{env['name']} references key {ref['key']!r}, absent from Secret {ref['name']!r} (has {sorted(keys)})"


# --------------------------------------------------------------------------------------------------
# 12. COLLECTION — a suite that runs nowhere is a claim, not a gate
# --------------------------------------------------------------------------------------------------


def _configured_testpaths() -> list[str]:
    import tomllib

    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]["testpaths"]


def test_every_workspace_test_directory_is_in_the_root_testpaths() -> None:
    """`testpaths` is EXPLICIT, so writing a suite and running it are two separate acts.

    This estate has already paid for the gap three times over: `services/catalog/tests` and
    `services/lineage/tests` were written and collected by nothing, and three landed regression suites
    (the catalog's replayed-commit door, lineage's external-source authz and its privileged-identity
    door) sat green-by-absence until 2026-08-09. Every one of them passed review — the directory is not
    where anybody looks.

    Derived from the tree rather than from a list, so the day a new member lands with tests the gate
    already covers it. The reverse direction is checked too: a testpath naming a directory that no
    longer exists makes pytest print `file or directory not found` and exit 4, which fails the whole
    run rather than the one suite.
    """
    configured = set(_configured_testpaths())
    present = {str(path.relative_to(REPO)) for glob in ("packages/*/tests", "services/*/tests") for path in REPO.glob(glob) if path.is_dir()}
    assert present, "no workspace test directories found — this gate would pass vacuously"

    uncollected = sorted(path for path in present if path not in configured)
    assert not uncollected, (
        f"these test directories exist and are collected by NOTHING: {uncollected}. Every run stays green "
        "while the suites inside them never execute — add them to [tool.pytest.ini_options] testpaths."
    )

    stale = sorted(path for path in configured if path.startswith(("packages/", "services/")) and not (REPO / path).is_dir())
    assert not stale, f"these testpaths name a directory that does not exist — pytest exits 4 and NO suite runs: {stale}"


def test_the_notifications_suite_is_collected_by_name() -> None:
    """The generic gate above cannot notice a services/ layout change that stops the glob matching, and
    the notification plane is exactly the kind of new member whose suites nobody would miss for a
    while. Named, so this one cannot go quiet either way."""
    assert "services/notifications/tests" in _configured_testpaths()


def test_the_notifications_pod_asks_for_a_sidecar_and_is_allowed_to_receive_one() -> None:
    """The sidecar's TWO halves on the one Deployment that cannot work without it.

    `test_every_dapr_annotated_pod_carries_the_injector_webhook_label` proves the correspondence for
    every pod that asks — and says nothing at all about a pod that never asks. That is the vacuous case
    this closes: the whole notification plane is actor state plus one bus subscription, so a
    Deployment rendered with no `dapr.io/*` annotations would come up healthy, serve its health surface
    and its gateway row, and fail every inbox route forever — on a pod whose probes stay green,
    because actor registration is process-local and cannot notice that no sidecar was injected.

    The app-id is asserted because it is not decoration: `lance-statestore`'s `scopes`, the
    `lineage-pubsub-notifications` component's subscriber list and the resiliency CRD all key on that
    exact string, and a mismatch disables actor hosting with no error anywhere.
    """
    rendered = _helm_template()

    pod = next(
        (
            (doc.get("spec") or {}).get("template")
            for doc in yaml.safe_load_all(rendered)
            if isinstance(doc, dict) and doc.get("kind") == "Deployment" and "notifications" in ((doc.get("metadata") or {}).get("name") or "")
        ),
        None,
    )
    assert pod is not None, "no notifications Deployment rendered — the service is not deployed at all"

    meta = pod.get("metadata") or {}
    annotations = meta.get("annotations") or {}
    assert annotations.get("dapr.io/enabled") == "true", f"the notifications pod does not ask for a sidecar: {sorted(annotations)}"
    assert annotations.get("dapr.io/app-id") == "notifications"
    assert annotations.get("dapr.io/app-port") == "8850"
    assert (meta.get("labels") or {}).get("dapr.io/enabled") == "true", (
        "the notifications pod asks for a sidecar by ANNOTATION but carries no injector LABEL — the "
        "fail-closed webhook skips it and it comes up un-injected, with every inbox route failing"
    )


# --------------------------------------------------------------------------------------------------
# 12. The notification plane's WIRING — the four facts that live in the chart and nowhere else
#
# The service's own suites prove what each route answers; none of them can see whether anything ever
# reaches those routes. Each guard below covers one address that is written down in exactly one place,
# read somewhere else, and whose omission produces a HEALTHY pod: a gateway proxying to itself, a
# subscription on a component that does not exist, an actor host with no state store, and a kubelet
# probing a path the app does not serve. Three of the four have already shipped once in this estate on
# a neighbouring service.
# --------------------------------------------------------------------------------------------------


def _rendered_docs(*set_values: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(_helm_template(*set_values)) if isinstance(doc, dict)]


def _collector_config(docs: list[dict]) -> dict:
    """The OTel Collector's whole rendered config, parsed out of its ConfigMap.

    Lifted so every collector gate locates the ConfigMap the SAME way. They used to disagree — one keyed
    on `"otlp" in config.yaml`, another on a processor's own name — so renaming a processor broke one gate
    and left the other passing on a config it could no longer find.
    """
    for doc in docs:
        data = doc.get("data") or {}
        if doc.get("kind") == "ConfigMap" and "config.yaml" in data and "otlp" in data["config.yaml"]:
            return yaml.safe_load(data["config.yaml"])
    return {}


def _collector_scrape_jobs(docs: list[dict]) -> list[dict]:
    """The OTel Collector's prometheus scrape_configs, parsed out of its ConfigMap."""
    config = _collector_config(docs)
    return list(config.get("receivers", {}).get("prometheus", {}).get("config", {}).get("scrape_configs", []))


def _greptimedb_config(docs: list[dict]) -> str:
    """The telemetry store's rendered `config.toml`, as text.

    Located by CONTENT, not by name: the subchart names its ConfigMap
    `<release>-greptimedb-standalone-config`, and a release-name change would silently return {} from a
    name match, turning every gate below green against a config it could no longer find.
    """
    for doc in docs:
        data = doc.get("data") or {}
        toml = data.get("config.toml", "")
        if doc.get("kind") == "ConfigMap" and "[storage]" in toml and "greptimedb" in toml:
            return toml
    return ""


#: The three settings that name a memory ceiling for a WORK-DRIVEN (non-cache) allocator in
#: GreptimeDB. Every one of them defaults to the literal string "unlimited".
_UNBOUNDED_BY_DEFAULT = ("memory_pool_size", "scan_memory_limit", "experimental_compaction_memory_limit")


def test_every_probe_path_the_chart_configures_is_excluded_from_tracing() -> None:
    """A kubelet polls twice a second forever, and every one of those probes was a traced request.

    MEASURED 2026-08-23 over three hours of live spans:

        GET /api/health http send   19,194
        GET /api/health              6,398
        GET /healthz http send       4,743
        GET /healthz                 1,581
                                    ------
                                    31,916 spans of pure liveness noise

    That is EIGHT TIMES the workflow keepalive everyone had noticed, and it dominated the RED metrics
    of every service whose probe path was not on the exclusion list. The counts differ 3:1 between a
    path and its `http send` twin because ASGI instrumentation emits a child span per send/receive, so
    each probe costs several spans rather than one.

    The cause is a list that drifted from the thing it is supposed to mirror. `rask.otelEnv` excluded
    `/livez,/readyz,/metrics`, but `chart/templates/fleet.yaml` pointed both probes at
    `healthPath | default "/api/health"`, and one service overrode it to `/healthz` — so the two
    services taking the DEFAULT were traced on every poll. (Those paths have since split to `/livez` +
    `/readyz` estate-wide, which is exactly the drift this gate refuses to trust a list about: it
    reads each pod's OWN rendered probes.)

    This gate is written against the rendered chart rather than a hardcoded list on purpose: it asks
    each first-party pod what path its OWN probes hit and requires the exclusion on that SAME pod to
    cover it. A new service, or a changed `healthPath`, is then caught by construction instead of by
    someone re-reading two files that have no reason to agree.
    """
    docs = _rendered_docs()

    uncovered: list[str] = []
    checked = 0
    for doc in docs:
        if doc.get("kind") not in ("Deployment", "StatefulSet"):
            continue
        spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
        for container in spec.get("containers") or []:
            env = {e.get("name"): e.get("value") for e in (container.get("env") or []) if isinstance(e, dict)}
            excluded_raw = env.get("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS")
            if excluded_raw is None:
                continue  # not an instrumented Python pod; nothing to promise
            excluded = [entry.strip() for entry in excluded_raw.split(",") if entry.strip()]
            paths = {(probe or {}).get("httpGet", {}).get("path") for probe in (container.get("livenessProbe"), container.get("readinessProbe"))} - {None}
            for path in sorted(paths):
                checked += 1
                # The instrumentation matches each entry as a regex SEARCHED against the url, so an
                # entry is covering when it appears anywhere in the path.
                if not any(entry.strip("/") and entry.strip("/") in path for entry in excluded):
                    uncovered.append(f"{doc.get('metadata', {}).get('name')} probes {path}, excluded={excluded}")

    assert checked, "no instrumented pod declared both a probe and an exclusion list — this gate is testing nothing"
    assert not uncovered, "these pods trace their own kubelet probes on every poll:\n  " + "\n  ".join(uncovered)


def test_the_workflow_keepalive_is_filtered_out_of_the_trace_stream() -> None:
    """`/TaskHubSidecarService/Hello` is the workflow engine pinging its own sidecar, and nothing reads it.

    RE-MEASURED 2026-08-23 (two earlier audits disagreed by 40 points on its share, so the number was
    taken fresh rather than inherited): 3,828 spans in three hours, the ninth-largest span name in the
    estate. It carries no information — it is a liveness ping between two processes in the same pod.

    Dropped at the Collector rather than at the app: the span is created by daprd, which this repo does
    not instrument and cannot configure per-span. The processor is scoped to the exact span name, not a
    prefix, so a real TaskHubSidecarService call is unaffected.

    Kept in proportion deliberately: this is the SMALL half of the trace-noise problem. The probe spans
    fixed alongside it were 31,916 over the same window — see
    `test_every_probe_path_the_chart_configures_is_excluded_from_tracing`.
    """
    config = _collector_config(_rendered_docs("observability.enabled=true"))
    processors = config.get("processors") or {}

    keepalive = [name for name, body in processors.items() if "TaskHubSidecarService" in yaml.safe_dump(body or {})]
    assert keepalive, (
        f"no processor drops the workflow keepalive — `/TaskHubSidecarService/Hello` reaches the store on every tick. processors are {sorted(processors)}"
    )

    traces = ((config.get("service") or {}).get("pipelines") or {}).get("traces") or {}
    wired = [p for p in (traces.get("processors") or []) if p in keepalive]
    assert wired, (
        f"the keepalive filter {keepalive} exists but is not in the traces pipeline "
        f"{traces.get('processors')} — a processor nothing references filters nothing."
    )


def test_the_collector_exporters_are_durable_and_their_storage_id_resolves() -> None:
    """A Collector restart during a store outage dropped whatever was in flight, silently.

    Both OTLP exporters declared `endpoint` and `headers` and nothing else, and all three pipelines
    ended in a bare `batch`. `batch` is not durability: it holds a batch in memory and forgets it if
    the process dies. That mattered concretely on 2026-08-23, when the store it writes to OOMKilled
    thirteen times — every one of those was a hole in the record that a queue would have ridden out.

    The second assertion is the one that earns its keep. A `sending_queue` naming a `storage:` id that
    is NOT in `service.extensions` passes `otelcol validate` with exit 0 and then kills the Collector
    at startup with `no storage client extension found` — taking the entire telemetry plane down to
    fix a durability gap. That failure mode is why this item was deferred rather than dashed off, so
    the gate checks the reference resolves rather than merely that a queue exists.

    Bounded on purpose: the queue is backed by an emptyDir shared with filelog's read offsets, so an
    unbounded queue would trade dropped telemetry for a filled node. `queue_size` and the volume's
    `sizeLimit` are both explicit for that reason.
    """
    docs = _rendered_docs("observability.enabled=true")
    config = _collector_config(docs)
    exporters = config.get("exporters") or {}
    otlp = {name: body or {} for name, body in exporters.items() if "otlphttp" in name}
    assert otlp, f"no otlphttp exporters found — exporters are {sorted(exporters)}"

    registered = set((config.get("service") or {}).get("extensions") or [])

    for name, body in sorted(otlp.items()):
        queue = body.get("sending_queue") or {}
        assert queue.get("enabled") is True, (
            f"exporter {name} has no enabled sending_queue — a Collector restart during a store outage drops what is in flight. keys are {sorted(body)}"
        )
        assert queue.get("queue_size"), f"exporter {name}'s sending_queue has no explicit queue_size"
        assert body.get("retry_on_failure", {}).get("enabled") is True, f"exporter {name} does not retry on failure"
        storage_id = queue.get("storage")
        if storage_id:
            assert storage_id in registered, (
                f"exporter {name}'s sending_queue names storage {storage_id!r}, which is NOT in "
                f"service.extensions {sorted(registered)}. This renders, passes `otelcol validate`, "
                f"and then CRASHES the Collector at startup with `no storage client extension found`."
            )


def test_the_telemetry_store_bounds_its_own_memory() -> None:
    """The estate's ONLY sink for logs, metrics and traces was configured to have no memory ceiling.

    MEASURED 2026-08-23: `rask-greptimedb-standalone-0` OOMKilled 13 times in a 7h27m window after
    running 6d21h clean, and the live `GET :4000/config` showed all three of the settings below as the
    literal string `"unlimited"` — `query.memory_pool_size`, `region_engine.mito.scan_memory_limit` and
    `region_engine.mito.experimental_compaction_memory_limit`. rask set NO GreptimeDB knobs at all: the
    mounted config.toml was 15 lines of `mode`, `[storage]` and `[logging]`.

    Why this is the defect and the trigger is not: the trigger was a new `ray-pods` scrape job adding
    4,099 series (commit 519ea5c4, live 04:36:45Z, first OOM 08:07:16Z). But a telemetry sink must
    survive whatever is written to it — a store that dies from new series is a store with no ceiling,
    and the NEXT thing to grow would have killed it just the same.

    Bounding is not the same as shrinking, and the direction matters: with a limit set, an oversized
    scan FAILS ITS QUERY (`scan_memory_on_exhausted = "fail"`) instead of taking the whole process
    down and losing every signal the estate has. One loud failure beats a silent hole in the record.

    The seam is `configToml` on the subchart — the ONLY channel it offers — and it was unset
    everywhere in the repo, which is why no lever could be delivered without adding this key.
    """
    toml = _greptimedb_config(_rendered_docs("observability.enabled=true"))
    assert toml, "no greptimedb config.toml rendered — the store's config gate is testing nothing"

    unbounded = [knob for knob in _UNBOUNDED_BY_DEFAULT if knob not in toml]
    assert not unbounded, (
        f"the telemetry store declares no ceiling for {unbounded}. Each defaults to the literal "
        f'"unlimited", so compaction, scans and queries may allocate without bound inside an 8Gi '
        f"container. Set them via `greptimedb-standalone.configToml` in chart/values.yaml."
    )

    # `[[region_engine]]`, NOT `[region_engine]`. GreptimeDB models region_engine as an ARRAY of
    # engine tables, so the map form renders perfectly valid TOML that the engine then refuses at
    # startup — `invalid type: map, expected a sequence`, exit 1, crash loop, store down. This gate
    # exists because a render-time TOML-validity check passed that bug straight through to the
    # cluster on 2026-08-23: parsing clean says nothing about the engine's schema.
    assert "[[region_engine]]" in toml, (
        "region_engine is declared as a map, not an array of engine tables. This renders valid TOML "
        "and then crash-loops GreptimeDB at startup with `invalid type: map, expected a sequence`."
    )
    assert "[region_engine.mito]" in toml, "no [region_engine.mito] section — the cache and compaction knobs live there"
    assert "page_cache_size" in toml, (
        "the mito cache sizes are left at GreptimeDB's large-machine defaults (~3.35GiB of caches, plus "
        "a 1GiB write buffer) inside an 8Gi container, so ~4.3GiB is committed before any query or "
        "compaction allocates a byte. Size the caches to the container."
    )


def test_the_ray_scrape_does_not_ship_the_per_operator_ray_data_firehose() -> None:
    """The Ray scrape job doubled the estate's series count in one step, and that is what broke the store.

    MEASURED: `scrape_samples_scraped` shows the job appearing on 2026-08-23 at 4,099 series against
    the whole prior estate's 3,250 (`dapr-sidecars` 3,186 + `dapr-control-plane` 64), and the store
    holds 288 `ray_*` metric tables of which 113 are `ray_data_*`.

    `ray_data_*` is Ray Data's PER-DATASET, PER-OPERATOR instrumentation — the series set grows with
    every dataset and operator a job creates, so it is unbounded by construction rather than merely
    large. Nothing in this estate reads it: the deployed Ray dashboard panels and the handover's own
    verification steps use node, cluster, serve and task series.

    Dropped at the Collector rather than at Ray, deliberately: the Ray cluster is operated elsewhere
    (open_ray_handover.md), so a fix that depends on its config is a fix this repo cannot land.
    """
    jobs = _collector_scrape_jobs(_rendered_docs())
    ray_jobs = [j for j in jobs if "ray" in str(j.get("job_name", ""))]
    assert ray_jobs, "no Ray scrape job at all"

    drops = [rc for rc in ray_jobs[0].get("metric_relabel_configs", []) if rc.get("action") == "drop"]
    dropped_patterns = " ".join(str(rc.get("regex", "")) for rc in drops)
    assert "ray_data_" in dropped_patterns, (
        "the Ray job ships every `ray_data_*` family (113 of them) — per-dataset, per-operator series "
        f"that nothing here reads. metric_relabel_configs drops are {drops or 'ABSENT'}."
    )


def test_ray_pods_are_a_scrape_target() -> None:
    """Nothing collected a single Ray series, and no rule or panel about Ray could ever have fired.

    The Collector's prometheus receiver had exactly two jobs, `dapr-sidecars` and `dapr-control-plane`,
    and BOTH `keep` on a `dapr.io/*` pod annotation at the first relabel step. A Ray pod carries
    neither, so it was dropped before any other rule ran — not "unscraped by oversight" but
    unreachable by construction. There is no PodMonitor path either: the chart has no
    prometheus-operator dependency, so such an object would be reconciled by nothing.

    The selector is NOT invented here. `chart/templates/network-policy.yaml` already selects Ray pods
    by `ray.io/is-ray-node: "yes"` and records why: "The chart's head template stamps no labels of its
    own; KubeRay stamps its marker on every Ray pod, so select that (component-label style can't reach
    operator-created pods)." Keying the scrape on anything else would be a second, divergent answer to
    a question this chart has already answered.
    """
    jobs = _collector_scrape_jobs(_rendered_docs())
    assert jobs, "the Collector renders no prometheus scrape_configs at all"

    ray_jobs = [j for j in jobs if "ray" in str(j.get("job_name", ""))]
    assert ray_jobs, f"no Ray scrape job — jobs are {[j.get('job_name') for j in jobs]}; every ray_* series is uncollected"

    keeps = [rc for rc in ray_jobs[0].get("relabel_configs", []) if rc.get("action") == "keep"]
    sources = {src for rc in keeps for src in rc.get("source_labels", [])}
    assert "__meta_kubernetes_pod_label_ray_io_is_ray_node" in sources, (
        f"the Ray job does not keep on KubeRay's own pod marker — keeps on {sources}. "
        "A dapr.io annotation or a component label cannot reach an operator-created pod."
    )


def test_the_ray_head_declares_the_port_its_metrics_are_served_on() -> None:
    """A scrape job that keeps on a container port NAME needs that port declared, or it matches nothing.

    KubeRay injects the metrics containerPort itself, but the chart's own head template lists only
    gcs/dashboard/client/serve — so the rendered pod spec advertises no metrics port and a port-name
    relabel silently produces zero targets. Declaring it here makes the head's telemetry surface a
    property of the manifest rather than of the operator's default.
    """
    docs = _rendered_docs("singleTenant.enabled=true")
    heads = [d for d in docs if d.get("kind") == "RayService"]
    assert heads, "no RayService rendered under singleTenant.enabled=true"

    containers = heads[0]["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]["containers"]
    names = {p.get("name") for c in containers for p in (c.get("ports") or [])}
    assert "metrics" in names, f"the Ray head declares ports {names} — no metrics port, so a port-name scrape matches nothing"


def _ray_head_env(docs: list[dict]) -> dict[str, str]:
    """The Ray head CONTAINER's env — not `serveConfigV2.runtime_env`, which is a different scope."""
    for doc in docs:
        if doc.get("kind") == "RayService":
            containers = doc["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]["containers"]
            return {e["name"]: str(e.get("value", "")) for c in containers for e in (c.get("env") or [])}
    return {}


EXTERNALISED: Final = (
    "observability.enabled=false",
    "observability.externalOtlpEndpoint=http://otel.observability:4318/v1/otlp",
)
TELEMETRY_POSTURES: Final = [
    pytest.param(("observability.enabled=true",), id="in-cluster"),
    pytest.param(EXTERNALISED, id="externalised"),
]


def _otlp_exporters(docs: list[dict]) -> dict[str, str]:
    """Every rendered container that exports OTLP, keyed `<workload>/<container>`.

    Covers the RayService head too — its containers hang off `rayClusterConfig`, not `spec.template`,
    which is exactly why a loop written over Deployments silently stops covering Ray.
    """
    out: dict[str, str] = {}
    for doc in docs:
        name = (doc.get("metadata") or {}).get("name", "?")
        if doc.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"}:
            spec = doc["spec"]["template"]["spec"]
        elif doc.get("kind") == "RayService":
            spec = doc["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]
        else:
            continue
        for container in spec.get("containers", []):
            for env in container.get("env") or []:
                if env.get("name") == "OTEL_EXPORTER_OTLP_ENDPOINT":
                    out[f"{name}/{container['name']}"] = str(env.get("value", ""))
    return out


def test_externalising_telemetry_does_not_silently_drop_ANY_pod() -> None:
    """The estate-wide twin of the Ray test below, and the reason that one was not enough.

    `lance.otelEnabled` is true for EITHER `observability.enabled` OR a set `externalOtlpEndpoint`.
    `rask.otelEnv` gated on `observability.enabled` alone, so the chart's own documented prod posture
    — ship OTLP off-cluster, deploy no in-cluster stack (`values-prod.yaml`) — rendered ZERO `OTEL_*`
    on the entire request-serving fleet while the lakehouse plane kept exporting. `rask-gateway`'s
    container came back as literally `env: null`.

    Nothing announced it: `setup_otel` returns False, every pod stays Ready, and because the gateway is
    the estate's only edge, every trace the lakehouse DID emit was rootless. The gap reads as "those
    services are idle".

    Asserted as a SET DIFFERENCE between two postures rather than a property of one render — that is
    what stops a future author narrowing it back to a single subject, which is precisely how the Ray
    version came to exist while six fleet workloads sat on the old gate unpinned.
    """
    default = set(_otlp_exporters(_rendered_docs("observability.enabled=true", "singleTenant.enabled=true")))
    externalised = set(_otlp_exporters(_rendered_docs(*EXTERNALISED, "singleTenant.enabled=true")))

    dropped = sorted(default - externalised)
    assert not dropped, (
        "externalising telemetry turned these containers' OTLP exporter OFF while the rest of the estate kept exporting:\n  "
        + "\n  ".join(dropped)
        + "\n\nGate them on `lance.otelEnabled` (observability.enabled OR externalOtlpEndpoint), not on "
        "`observability.enabled` alone."
    )


@pytest.mark.parametrize("posture", TELEMETRY_POSTURES)
def test_no_pod_exports_to_a_service_the_render_does_not_create(posture: tuple[str, ...]) -> None:
    """An OTLP endpoint naming an in-cluster Service that the SAME render omits is a silent total loss.

    `lance.otlpEndpoint` preferred the in-cluster Collector whenever `otelCollector.enabled` (default
    true), but the Collector only renders under `and $o.enabled $c.enabled`. So with telemetry
    externalised, ten lakehouse pods were aimed at `<release>-otel-collector:4318` — a Service the same
    render does not create — and the operator's chosen `externalOtlpEndpoint` was discarded entirely.

    It fails loudly nowhere. The SDK retries a refused connection with in-process exponential backoff,
    which is the exact pathology `otel.py`'s docstring records costing ~2.7s per unit test in CI.

    Only BARE hostnames are checked: a dotted name is an FQDN or an off-cluster host and is the
    operator's business, not this chart's.
    """
    docs = _rendered_docs(*posture, "singleTenant.enabled=true")
    services = {d["metadata"]["name"] for d in docs if d.get("kind") == "Service"}

    dangling = []
    for who, endpoint in sorted(_otlp_exporters(docs).items()):
        host = (urlparse(endpoint).hostname or "").strip()
        if not host or "." in host:
            continue
        if host not in services:
            dangling.append(f"{who} -> {endpoint}")

    assert not dangling, (
        "these containers export OTLP to an in-cluster Service the SAME render does not create:\n  "
        + "\n  ".join(dangling)
        + f"\n\nServices this render DOES create: {sorted(services)}\n"
        "The endpoint branch must share the Collector's own render gate, or it promises a backend that was never deployed."
    )


def test_the_estate_emits_ONE_otel_resource_attribute_schema() -> None:
    """Two resource schemas means every cross-plane query silently sees half the estate.

    The fleet emitted `service.namespace=rask,deployment.environment=<ns>` while the lakehouse and Ray
    emitted `service.namespace=lance-ns,deployment.environment.name=<env>,service.version=<ver>` — zero
    key overlap. `deployment.environment` was RENAMED to `deployment.environment.name` in OTel semconv
    v1.27.0 and the old key is marked deprecated, so the fleet was also emitting the dead name.

    This is not theoretical: GreptimeDB has already materialised both as separate physical columns in
    `opentelemetry_traces`, so a filter, join or dashboard variable written against either name is
    blind to the other plane's spans.

    Asserted on the KEY SET, not the values — `service.name` legitimately differs per pod; the schema
    must not.
    """
    docs = _rendered_docs("observability.enabled=true", "singleTenant.enabled=true")

    schemas: dict[frozenset[str], list[str]] = {}
    for doc in docs:
        name = (doc.get("metadata") or {}).get("name", "?")
        if doc.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"}:
            spec = doc["spec"]["template"]["spec"]
        elif doc.get("kind") == "RayService":
            spec = doc["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]
        else:
            continue
        for container in spec.get("containers", []):
            for env in container.get("env") or []:
                if env.get("name") != "OTEL_RESOURCE_ATTRIBUTES":
                    continue
                keys = frozenset(pair.split("=", 1)[0] for pair in str(env.get("value", "")).split(",") if "=" in pair)
                schemas.setdefault(keys, []).append(f"{name}/{container['name']}")

    assert schemas, "no workload declares OTEL_RESOURCE_ATTRIBUTES — this gate would pass vacuously"
    rendered = "\n  ".join(f"{sorted(keys)} <- {sorted(who)}" for keys, who in schemas.items())
    assert len(schemas) == 1, f"the estate emits more than one OTel resource-attribute schema, so no cross-plane query can see all of it:\n  {rendered}"


def test_ray_telemetry_is_release_derived_like_every_other_pods() -> None:
    """The Ray OTLP block hardcoded the release name while the whole rest of the chart derives it.

    `rask.otelEnv`'s own comment records having fixed exactly this defect elsewhere — "the
    release-derived Greptime host (was hardcoded "rask-greptimedb-standalone", which ignored the
    release name)" — and it regressed in a fourth, hand-rolled copy inside `serveConfigV2`. Proven by
    render before the fix: every other telemetry consumer emitted `release-name-otel-collector:4318`
    while the RayService emitted `rask-greptimedb-standalone:4000`, i.e. a Service the same render does
    not create. A release installed under any other name sent Ray telemetry into a DNS name that does
    not resolve, and the only symptom was an export failure logged inside a Ray worker — which nothing
    collects.
    """
    docs = _rendered_docs("singleTenant.enabled=true")
    env = _ray_head_env(docs)
    endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    assert endpoint, (
        "the Ray HEAD CONTAINER carries no OTLP endpoint — a serveConfigV2-only block reaches one app's replicas, not the head, GCS, raylet, dashboard or Serve controller"
    )
    assert "rask-" not in endpoint, f"Ray's OTLP endpoint {endpoint!r} hardcodes a release name"

    others = {
        str(e.get("value", ""))
        for d in docs
        if d.get("kind") == "Deployment"
        for c in d["spec"]["template"]["spec"].get("containers", [])
        for e in (c.get("env") or [])
        if e.get("name") == "OTEL_EXPORTER_OTLP_ENDPOINT"
    }
    assert others == {endpoint}, f"Ray exports to {endpoint!r} while the rest of the estate exports to {others} — one estate, two backends"


def test_the_platform_chart_does_not_name_a_WORKLOAD_in_rays_telemetry_identity() -> None:
    """`OTEL_SERVICE_NAME: "ray-htrflow"` put one modality's name on the shared Ray plane's identity.

    CLAUDE.md is explicit that no service, schema or chart may know a workload's name: every runner is
    sealed, and the platform must read the same for audio, text, image and one nobody has written yet.
    A workload literal here means every span and every metric from the shared cluster arrives labelled
    as that one workload, which makes per-workload attribution impossible — the identity has to come
    from the platform, with the workload riding in Serve's own `application`/`deployment` labels.
    """
    env = _ray_head_env(_rendered_docs("singleTenant.enabled=true"))
    service_name = env.get("OTEL_SERVICE_NAME", "")

    assert service_name, "the Ray head declares no OTEL_SERVICE_NAME, so its telemetry lands as unknown_service"
    assert "htrflow" not in service_name.lower(), f"OTEL_SERVICE_NAME={service_name!r} names a workload in the platform chart"


def test_externalising_telemetry_does_not_silently_drop_ray() -> None:
    """The Ray block gated on `observability.enabled`, and so did the whole FLEET — undetected until 2026-08-23.

    That second half is why this test was not enough on its own. Its premise used to read "the rest of
    the estate gates on `lance.otelEnabled`", which was simply untrue of `rask.otelEnv`; stating it as
    settled is what let six fleet workloads sit on the narrow gate, pinned by nothing, while this test
    passed. `test_externalising_telemetry_does_not_silently_drop_ANY_pod` is now the estate-wide gate,
    and this one survives because the Ray head's containers live under `rayClusterConfig` — a spec path
    a Deployment loop silently misses.

    `lance.otelEnabled` is true for EITHER `observability.enabled` OR a set `externalOtlpEndpoint`, and
    its own comment says why: "otherwise externalize silently emits nothing". Ray was on the narrower
    gate, so the documented production posture — ship OTLP off-cluster, deploy no in-cluster stack —
    turned Ray's telemetry off while every other pod kept exporting. The gap then reads as "Ray is
    idle", not as "Ray is uninstrumented".
    """
    docs = _rendered_docs(
        "singleTenant.enabled=true",
        "observability.enabled=false",
        "observability.externalOtlpEndpoint=http://otel.observability:4318/v1/otlp",
    )
    env = _ray_head_env(docs)
    assert env.get("OTEL_EXPORTER_OTLP_ENDPOINT"), "externalising telemetry left the Ray head with no OTLP endpoint at all"


def test_rays_two_tracing_switches_are_not_cross_wired() -> None:
    """Ray has TWO tracing planes with DIFFERENT contracts, and swapping them fails SILENTLY.

    The core hook (`rayStartParams.tracing-startup-hook`) takes no arguments and returns None — it
    sets the global provider. The Serve hook (`RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH`) takes no
    arguments and RETURNS a list of SpanProcessor, because Serve builds the provider itself. Point
    either env at the other's function and Serve catches the resulting error, logs "the proxy/replica
    will continue running", and keeps serving — nothing goes unhealthy and no span is ever produced.

    Also pins HEAD-ONLY for the core hook: it is persisted to GCS internal KV by
    `start_head_processes()` and read from there by every connecting process, so the same key on a
    workerGroupSpec is a no-op that reads like configuration.
    """
    docs = _rendered_docs("singleTenant.enabled=true")
    ray = next((d for d in docs if d.get("kind") == "RayService"), None)
    assert ray is not None, "no RayService rendered under singleTenant.enabled=true"

    head = ray["spec"]["rayClusterConfig"]["headGroupSpec"]
    assert head["rayStartParams"].get("tracing-startup-hook") == "service_kit.ray_tracing:setup_tracing", (
        f"core tracing hook is {head['rayStartParams'].get('tracing-startup-hook')!r} — Ray Core emits no spans without it"
    )

    env = {e["name"]: str(e.get("value", "")) for c in head["template"]["spec"]["containers"] for e in (c.get("env") or [])}
    assert env.get("RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH") == "service_kit.ray_tracing:serve_span_processors", (
        "the Serve exporter path is wrong — a gateway-originated trace dies at the Serve door, silently"
    )
    assert env["RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH"] != head["rayStartParams"]["tracing-startup-hook"], (
        "the two hooks are CROSS-WIRED: Serve needs a function returning list[SpanProcessor], the core hook returns None"
    )
    assert float(env.get("RAY_SERVE_TRACING_SAMPLING_RATIO", "0.01")) > 0.01, (
        "Serve trace sampling is at or below the upstream default of 0.01 — a ten-request smoke test yields zero spans and reads as broken"
    )


def _perses_dashboards(docs: list[dict]) -> dict[str, dict]:
    """{key: parsed dashboard} from the Perses ConfigMap."""
    import json as _json

    for doc in docs:
        data = doc.get("data") or {}
        if doc.get("kind") == "ConfigMap" and "fleet-red.json" in data:
            return {k: _json.loads(v) for k, v in data.items() if k.endswith(".json")}
    return {}


def test_a_ray_dashboard_exists_and_uses_rays_own_promql_correctly() -> None:
    """Six dashboards existed and not one had a single Ray panel — on the cluster the entire
    bronze->silver->gold cascade runs on.

    The queries are ported from Ray's own shipped panels rather than invented, because three of its
    metric shapes are counter-intuitive and each produces a plausible-looking wrong answer:

      * `ray_node_cpu_utilization` is a PERCENT (0-100), not a ratio, so cores-in-use needs
        `* ray_node_cpu_count / 100`.
      * object-store CAPACITY lives in `ray_resources{Name="object_store_memory"}`, not in any
        `ray_node_*` series, so a usage percentage computed from `ray_node_*` alone has no denominator.
      * `ray_tasks` is a GAUGE and must never be `rate()`d — Ray's own panel pairs `max_over_time`
        for terminal states with `clamp_min` for live ones, because these gauges are eventually
        consistent.

    And the agnosticism rule is enforced, not merely intended: `application` and `deployment` are
    label VALUES that today carry a workload name (chart/values.yaml serveRoutePrefix/importPath), so
    every Serve panel must GROUP BY them and none may filter on one.
    """
    dashboards = _perses_dashboards(_rendered_docs())
    assert dashboards, "no Perses dashboard ConfigMap rendered"

    ray = dashboards.get("ray.json")
    assert ray is not None, f"no Ray dashboard — dashboards are {sorted(dashboards)}"

    # `queries` sits on the PANEL spec, beside `plugin` — not inside it. Perses nests the chart plugin
    # and the query list as siblings, and reading the wrong one yields an empty list rather than an
    # error, which is how a gate like this passes vacuously.
    queries = [q["spec"]["plugin"]["spec"]["query"] for panel in ray["spec"]["panels"].values() for q in panel["spec"].get("queries", [])]
    assert queries, "the Ray dashboard renders no queries"
    blob = " ".join(queries)

    assert "ray_" in blob, "no ray_* series is queried at all"
    assert "rate(ray_tasks" not in blob, "ray_tasks is a GAUGE — rate() over it is meaningless"
    for workload in ("htrflow", "htr", "asr", "diarize", "voiceprint"):
        assert f'"{workload}"' not in blob, f"a Ray panel filters on the {workload!r} workload — group by application/deployment instead"


def test_the_chart_can_express_a_SECOND_serve_application() -> None:
    """`serveConfigV2` hardcoded `- name: htrflow`, so the platform chart could express exactly ONE
    Serve application and knew its name.

    CLAUDE.md is explicit that no service, schema or chart may know a workload's name: every runner is
    sealed and the platform must read the same for audio, text, image and one nobody has written yet.
    A single hardcoded application is the strongest form of that violation — a second workload could
    not be deployed without editing a platform template, which is precisely what "a workload reaches
    the platform as config" is supposed to prevent.
    """
    import yaml as _yaml

    docs = _rendered_docs(
        "singleTenant.enabled=true",
        "ray.serveApplications[0].name=alpha",
        "ray.serveApplications[0].importPath=runner.alpha:app",
        "ray.serveApplications[0].routePrefix=/alpha",
        "ray.serveApplications[1].name=beta",
        "ray.serveApplications[1].importPath=runner.beta:app",
        "ray.serveApplications[1].routePrefix=/beta",
    )
    ray = next((d for d in docs if d.get("kind") == "RayService"), None)
    assert ray is not None, "no RayService rendered"

    apps = _yaml.safe_load(ray["spec"]["serveConfigV2"])["applications"]
    names = [a["name"] for a in apps]
    assert names == ["alpha", "beta"], f"the chart rendered {names} — it cannot express two Serve applications"
    assert "htrflow" not in _yaml.dump(apps), "a workload literal survived into a render that never asked for it"


def test_the_gpu_coherence_guard_speaks_no_modality() -> None:
    """The render-time `fail` is a PLATFORM guard rail, and it named both a workload and a MODEL.

    An operator on an audio estate hitting a GPU misconfiguration was told about `htrflow` and
    `TrOCR` — neither of which they run. A platform error that names someone else's modality is worse
    than a vague one: it sends the reader looking for a component that is not there.
    """
    body = (REPO / "chart/templates/gpu-coherence.yaml").read_text(encoding="utf-8")
    for literal in ("htrflow", "TrOCR"):
        assert literal not in body, f"the GPU coherence guard names {literal!r} — the platform knows no workload"


def test_the_chart_tells_kuberay_the_ray_version_the_image_actually_ships() -> None:
    """Three answers in one estate, and the chart held the wrong one.

    `chart/values.yaml` declared `rayVersion: "2.56.1"`; the root `uv.lock` resolves ray 2.57.0, and
    `packages/ratch` REQUIRES `ray[data,default]>=2.57`. That matters because
    `.docker/ray-cluster.dockerfile` builds the image with `uv sync --package ray-cluster-env` FROM THAT LOCK —
    so the chart was telling the KubeRay operator one version while the container ran another.

    KubeRay uses `rayVersion` for its own compatibility gating (the chart's auth block already notes
    `>= 2.52.0` is required for `spec.authOptions`), so a stale value is not cosmetic: it is the
    operator reasoning about a cluster that does not exist. And the drift is silent — nothing compared
    the two until this test.

    The lock is the source of truth because it is what the image is built from. If they disagree, the
    chart is the one that is wrong.
    """
    import re

    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    match = re.search(r'\[\[package\]\]\nname = "ray"\nversion = "([^"]+)"', lock)
    assert match, "could not find the resolved ray version in uv.lock"
    resolved = match.group(1)

    values = (REPO / "chart/values.yaml").read_text(encoding="utf-8")
    declared = re.search(r'^\s*rayVersion:\s*"([^"]+)"', values, re.MULTILINE)
    assert declared, "chart/values.yaml declares no rayVersion"

    assert declared.group(1) == resolved, (
        f"chart rayVersion is {declared.group(1)!r} but the image is built from a lock resolving "
        f"{resolved!r} (.docker/ray-cluster.dockerfile: uv sync --package ray-cluster-env). KubeRay is being "
        f"told about a cluster that does not exist."
    )


def test_dapr_sidecar_spans_actually_have_an_exporter() -> None:
    """THE CRUX. `samplingRate` is a SAMPLING knob, not an enable switch.

    The `lance-tracing` Configuration set `spec.tracing.samplingRate: "1"` and nothing else — no
    `otel:`, no `zipkin:`, no `stdout: true`. Per the Dapr v1.18.1 contract that pinned by
    chart/Chart.yaml, a tracing stanza with no exporter registers the NullExporter: every daprd
    sidecar span is created, sampled at 100%, has its context propagated — and is then discarded by an
    `ExportSpans` that returns nil. No log line, no error, no metric. It looks exactly like a collector
    that is down.

    So every Dapr hop in the estate — service invocation, pub/sub publish and delivery, actor calls,
    input bindings — produced no span anywhere, while three files asserted the opposite
    (observability.yaml's own comment, chart/values.yaml, docs/MEDALLION.md).

    One correction to that sentence, which used to end "...input bindings, workflow steps": WORKFLOW
    STEPS were never in the hole. The Python SDK creates its own `activity: <name>` and orchestration
    spans through the APP's tracer, so they reached the store the whole time on the app's exporter,
    independent of anything the sidecar did or did not export. What the missing stanza cost there was
    the daprd HALF of each hop — the grpc call into the sidecar that carries an activity — not the
    activity itself. Verified after this landed: all four workflow span kinds now arrive.

    The stated reason for omitting it was also wrong on its own terms: Dapr's `otel.headers` accepts
    arbitrary pairs, and more to the point the estate RUNS a Collector whose `otlp` receiver already
    listens on 4317 and 4318 and which adds GreptimeDB's headers itself. The receiving half was built
    and idle.

    The suite already had three tests over this object and all three were about the WORKFLOW half —
    one even asserts `"tracing" not in spec` when telemetry is off, so it knew the stanza existed and
    still never checked that it could export. There is a render-time `fail` guard against a malformed
    workflow retention value; there was none against a tracing stanza that exports into a black hole,
    which is the strictly more total silent failure.
    """
    spec = _lance_tracing_config(_helm_template())
    assert spec is not None, "the lance-tracing Configuration does not render"

    tracing = spec.get("tracing")
    assert tracing, "no tracing stanza at all — sidecars fall back to Dapr's defaults"

    exporters = {"otel", "zipkin", "stdout"} & set(tracing)
    assert exporters, (
        f"tracing is configured with NO exporter (keys: {sorted(tracing)}). samplingRate alone means "
        "daprd registers a NullExporter: spans are created, sampled and propagated, then dropped "
        "silently — indistinguishable from a dead collector."
    )

    if "otel" in exporters:
        otel = tracing["otel"]
        assert otel.get("endpointAddress"), "otel.endpointAddress is required by the CRD"
        # isSecure DEFAULTS TO TRUE upstream. Omitting it against the plaintext in-cluster Collector
        # makes every sidecar attempt TLS and fail — which fails soft, so it looks like no tracing.
        assert otel.get("isSecure") is False, f"otel.isSecure must be explicitly false for a plaintext Collector, got {otel.get('isSecure')!r}"
        assert otel.get("protocol") in {"http", "grpc"}, (
            f"otel.protocol must be exactly 'http' or 'grpc' — anything else is a fatal daprd startup error, got {otel.get('protocol')!r}"
        )
        # Dapr sets NO url path, so `http` posts to <endpoint>/v1/traces while GreptimeDB ingests at
        # /v1/otlp/v1/traces — a prefix Dapr cannot express. The Collector is the only correct target.
        assert "greptime" not in str(otel["endpointAddress"]).lower(), (
            "Dapr cannot express GreptimeDB's /v1/otlp path prefix — point it at the Collector, which adds the headers"
        )


def _fleet_config(docs: list[dict]) -> dict[str, str]:
    """The fleet ConfigMap — the one carrying `RASK_API_PREFIX` and the gateway's upstream addresses."""
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and "RASK_API_PREFIX" in (doc.get("data") or {}):
            return doc["data"]
    raise AssertionError("no fleet ConfigMap rendered — every gateway upstream would fall back to a localhost default")


def _notifications_container(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") == "Deployment" and "notifications" in ((doc.get("metadata") or {}).get("name") or ""):
            return doc["spec"]["template"]["spec"]["containers"][0]
    raise AssertionError("no notifications Deployment rendered")


def test_the_gateway_learns_where_the_inbox_lives_rather_than_proxying_to_itself() -> None:
    """`RASK_NOTIFICATIONS_URL` is the only thing standing between the row and a self-proxy.

    The gateway's row defaults to `http://127.0.0.1:8850` — inside the gateway POD that address is the
    gateway, where no inbox route exists, so every call answers a 404 indistinguishable from an
    unrouted path while the service is up and healthy one Service name away. This is not a
    hypothetical: `RASK_INGEST_URL` shipped missing and the configmap now carries a comment saying so
    (`chart/templates/configmap.yaml`), which is a comment and not a gate.

    The port is compared against the container's OWN port rather than a literal: the failure this
    catches second is the address being right and the port stale.
    """
    docs = _rendered_docs()
    url = _fleet_config(docs).get("RASK_NOTIFICATIONS_URL")
    port = _notifications_container(docs)["ports"][0]["containerPort"]

    assert url, "the chart renders no RASK_NOTIFICATIONS_URL — the gateway's /api/notifications row proxies to the gateway itself"
    assert "127.0.0.1" not in url and "localhost" not in url, f"the gateway is pointed at itself: {url}"
    assert url.endswith(f":{port}"), f"the gateway addresses {url} while the pod listens on {port}"


def test_the_inbox_subscribes_on_a_component_the_chart_actually_renders() -> None:
    """A subscription names its pubsub component by string, and a name that resolves to nothing is a
    STARTUP error the pod survives: daprd logs it, the app serves its health surface and its gateway
    row, and not one lineage event is ever delivered.

    Both ends are asserted because they fail in different deployments. The chart's value is what the
    pod reads; the app's DEFAULT is what a dev run without the ConfigMap reads, and the two drifting
    apart is how a subscription works in one environment and is silently dead in the other. Same shape
    as `test_user_state_store_default_matches_the_component_the_catalog_is_scoped_to`, for the same
    reason: the name is a coordinate, and nothing else compares its two ends.
    """
    from notifications.api.settings import IngressSettings

    docs = _rendered_docs()
    configured = _fleet_config(docs)["RASK_NOTIFICATIONS_PUBSUB"]
    component = next((d for d in docs if d.get("kind") == "Component" and d["metadata"]["name"] == configured), None)

    assert component is not None, f"the inbox is configured to subscribe on {configured!r}, which the chart renders no Component for"
    assert IngressSettings.model_fields["pubsub"].default == configured, (
        f"the service defaults to {IngressSettings.model_fields['pubsub'].default!r} while the chart configures {configured!r} — "
        "a dev run and a deployed pod would subscribe on different components"
    )
    # `scopes` is a ROOT field of a Dapr Component, not part of `spec`: an unscoped app-id gets
    # "component not found" from its sidecar, which is the same silence as a missing component.
    assert "notifications" in (component.get("scopes") or []), f"{configured} is not scoped to notifications — its sidecar refuses to load it"


def test_the_inbox_subscription_starts_from_new_and_keeps_a_durable_cursor() -> None:
    """The two consumer settings that decide what a rollout does to a badge.

    `deliverPolicy: all` is right for lineage — replaying the retained stream into an idempotent MERGE
    is its outage-durability story — and catastrophic here: an inbox would re-notify a week of history
    on every deploy, which is a badge that lies loudest right after a release. `durableName` is the
    other half: without it the queue-group consumer is deleted with its last member, so a run that
    terminated while this app was down is skipped on reconnect and its author is never told.

    Both are chart-side facts with no code that can check them, and the pair is what makes the
    subscription correct — `new` alone loses events, durable alone replays them.
    """
    docs = _rendered_docs()
    configured = _fleet_config(docs)["RASK_NOTIFICATIONS_PUBSUB"]
    component = next(d for d in docs if d.get("kind") == "Component" and d["metadata"]["name"] == configured)
    settings = {m["name"]: m.get("value") for m in component["spec"]["metadata"]}

    assert settings.get("deliverPolicy") == "new", f"the inbox consumer is {settings.get('deliverPolicy')!r} — a rollout would re-notify the retained backlog"
    assert settings.get("durableName"), "the inbox consumer is ephemeral — a run that terminated while the pod was down never reaches its author"
    assert settings.get("queueGroupName") == "notifications", (
        "without its own queue group every replica receives every message (jetstream sets a DeliverGroup only when "
        "queueGroupName is non-empty), so a scaled deployment delivers each notification once per pod"
    )


def test_the_actor_state_store_is_scoped_to_the_service_whose_whole_state_it_is() -> None:
    """`lance-statestore` carries `actorStateStore: "true"`, and an app-id missing from its `scopes`
    gets "Actor state store not configured - actor hosting disabled" from its own sidecar.

    The notification plane is nothing BUT actor state — one InboxActor per subject holding the pointer
    rows and the compaction reminder — so unscoped it fails at every INVOCATION and at no earlier
    point: `ActorRuntime.register_actor` is process-local and cannot notice a missing scope, so
    `actors_registered` is True, `require_actor_plane` admits the request, and the sidecar's refusal
    reaches the caller untranslated. The pod is Ready with a permanently empty bell. Nothing crashes
    and nothing else would notice.

    Found by property rather than by name: the store is whichever Component declares itself the actor
    state store, so a rename cannot make this pass vacuously.
    """
    docs = _rendered_docs()
    stores = [
        doc
        for doc in docs
        if doc.get("kind") == "Component"
        and any(m.get("name") == "actorStateStore" and str(m.get("value")).lower() == "true" for m in (doc["spec"].get("metadata") or []))
    ]

    assert len(stores) == 1, f"expected exactly one actor state store, found {[d['metadata']['name'] for d in stores]}"
    assert "notifications" in (stores[0].get("scopes") or []), (
        f"{stores[0]['metadata']['name']} is not scoped to notifications — the sidecar disables actor hosting and every inbox route fails "
        "on a pod that reports itself healthy"
    )


def test_a_ray_SUBMISSION_is_never_pre_gated_on_cluster_capacity() -> None:
    """B2. Accept-time gates may refuse on unit count, deadline and FGA — NEVER on capacity.

    Custom resource labels are LOGICAL: an autoscaler can satisfy them on demand, and a worker type
    advertising them may be scaled to zero at submission time. A pre-submission check against a static
    cluster snapshot therefore rejects work the scheduler would have queued and satisfied — and it
    fails in the worst direction, because the rejection looks like a capacity problem rather than a
    bug in the gate.

    rask is on the right side today (`ray_submit.py` POSTs straight to /api/jobs/) and the doc records
    that it will be tempted off it once bronze->silver starts queueing. This is the gate that notices.

    Scoped to the SUBMIT path only. `ray_kit/dashboard.py` reads /api/cluster_status legitimately — it
    is the compute service's read-only introspection view, which is a different job from deciding
    whether to submit.
    """
    submit_path = [
        REPO / "services" / "medallion" / "src" / "medallion" / "services" / "ray_submit.py",
        REPO / "services" / "medallion" / "src" / "medallion" / "workflow.py",
        REPO / "packages" / "ray-kit" / "src" / "ray_kit" / "submit.py",
    ]
    banned = ("cluster_status", "available_resources", "cluster_resources", "ray.nodes", "/nodes")
    for path in submit_path:
        if not path.exists():
            continue
        body = path.read_text()
        for needle in banned:
            assert needle not in body, (
                f"{path.name} reads cluster capacity ({needle!r}) on the SUBMIT path. A pre-submission "
                f"capacity gate refuses work an autoscaler would have satisfied; refuse on unit count, "
                f"deadline or FGA instead."
            )


def test_the_ray_cluster_image_can_read_the_lakehouse() -> None:
    """The deployed Ray image must carry the platform compute stack, at the FLEET's versions.

    THE DEFECT THIS CLOSES. `.docker/ray-cluster.dockerfile` used to resolve from
    `runners/htr/uv.lock` — a sealed WORKLOAD lock containing no package matching lance. Every stage
    job the medallion submitted died `exit 1 / ModuleNotFoundError: No module named 'lance'`, and the
    Ray lane had to be pinned off to keep the cascade working at all.

    THE VERSIONS ARE THE POINT, not merely the presence. This image reads and writes the SAME blob-v2
    datasets the fleet writes, so a split is a correctness bug: measured at pylance 8.0.0 against a
    9.0.0-written dataset, the whole row-aligned blob read path raised on every projection and the
    descriptor validity mask silently mis-reported payload presence.

    WHAT CHANGED, and why this is stricter rather than looser. The trio used to be installed with
    hand-written pins, and this test compared them against `ray-lance.dockerfile`'s hand-written
    pins — two literals kept equal by a test. The image now resolves `packages/ray-cluster-env` —
    the deps-only member that NAMES the platform compute environment (open_ray-kernel.md move 13; it
    replaced `--package ratch`, whose dependency list the image had been inheriting by accident) —
    from the ROOT lock, so it and the fleet agree BY CONSTRUCTION and there is no pin to drift. So
    the assertion moves to the mechanism: the image builds from the root lock, and that member
    really does carry the stack.
    """
    dockerfile = (REPO / ".docker" / "ray-cluster.dockerfile").read_text()

    assert "uv sync --package ray-cluster-env" in dockerfile, (
        "the deployed Ray image no longer builds the platform environment from the root lock. Without "
        "it the cluster cannot read the lakehouse at all (ModuleNotFoundError on every stage job), or "
        "reads it at a different pylance than the fleet writes with — which corrupts the blob read "
        "path silently."
    )
    assert "--frozen" in dockerfile and "--locked" in dockerfile, "the root-lock build must be frozen/locked, or the image can float off the fleet's versions"

    env_member = (REPO / "packages" / "ray-cluster-env" / "pyproject.toml").read_text()
    for package in ("pylance", "lance-ray", "pyarrow", "ray["):
        assert package in env_member, (
            f"packages/ray-cluster-env no longer declares {package!r}, so the Ray image would silently stop carrying it. "
            "This test reads that member as the definition of the platform compute stack."
        )


def test_the_ray_lane_is_ON_now_that_the_cluster_and_the_job_CONTRACT_both_exist() -> None:
    """The Ray lane defaults ON. Both blockers that kept it off are closed, and both were MEASURED.

    This gate has now asserted each answer in turn, which is the point of writing the reason down
    rather than the verdict. It first demanded ON (argument: Ray is the compute plane, and an off lane
    makes S1's watcher dead code). It was flipped to demand OFF when that configuration was measured
    and could not work. It demands ON again because the two named blockers were fixed — not because
    the original argument came back into fashion.

    **Blocker 1 — the image — was never real in the form it was written down.** The OFF docstring said
    `rayAddress` derives to a head running `.docker/ray-cluster.dockerfile`, "the HTR/CUDA image built
    from `runners/htr` ... torch and htrflow and no pylance", and that every stage job died
    `ModuleNotFoundError: No module named 'lance'`. That was true of the image AT THE TIME and stopped
    being true on 2026-08-17, when the cluster image started installing the platform compute trio
    beside the workload's lock. Read from the running estate 2026-08-24, the chart's own KubeRay head
    answers `import lance` with **pylance 10.0.0** and imports **lance_ray**. Pinned independently by
    `test_the_ray_cluster_image_can_read_the_lakehouse`.

    **Blocker 2 — the job env contract — is the one that actually gated this, and it is closed.**
    A capable image is necessary and not sufficient: `ray_submit.py` shipped a FIXED `env_vars` dict,
    so a mover could reach a working cluster and still not describe its own work — a second workload
    either reused the first one's variable names or forced a platform edit. `submit_stage_job` now
    resolves `entrypoint`, `params` and `code_version` from the LANE DECLARATION
    (`services/lane.py::resolve_lane_async`, which REFUSES a named-but-undeclared lane rather than
    falling back to the chart's program), and namespaces the workload's half as `RASK_PARAM_<key>`.
    The prefix is applied at the submitter, never trusted from config, so a lane cannot choose a name
    that collides with `S3_SECRET`, `LINEAGE_JSON` or an `OTEL_*` key.

    **And the whole path was driven, not reasoned about.** 2026-08-24, against the live release with
    the lane on: `stage-ray-silver-3beb0dd2fcbd44a0b5e356dc2aeaaa39-e4b061d30061` ran as a Dapr
    Workflow, the watcher polled `rask-ray-head-svc:8265/api/jobs/<id>` 200, the orchestration reported
    `COMPLETED`, and the mover logged `medallion_stage_moved`.

    `compute` stays ON and is still asserted: the settings validator refuses `ray` without it, and it
    is what the in-process lane needs.

    NOT asserted here, and deliberately: that an OFF->ON *upgrade* is safe. It is not, on its own —
    daprd caches the actor state store at sidecar start, so movers enabled in the same upgrade that
    adds them to `lance-statestore`'s scopes come up against the old list. That is an operational
    ordering property of a live release, not a property of the rendered manifest, so it lives in the
    values comment where the operator reads it. A FRESH install renders both together and is fine.
    """
    docs = _rendered_docs()
    movers = [
        c
        for doc in docs
        if doc.get("kind") == "Deployment"
        for c in doc["spec"]["template"]["spec"]["containers"]
        if any(e.get("name") == "MEDALLION_FROM_URI" for e in (c.get("env") or []))
    ]
    assert movers, "no medallion movers rendered — the fixture cannot prove anything"
    for container in movers:
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env.get("MEDALLION_COMPUTE_ENABLED") == "true", "ray without compute fails the settings validator at boot"
        assert env.get("MEDALLION_RAY_ENABLED") == "true", (
            "the Ray lane is OFF by default. Both blockers are closed (a Lance-capable cluster image, "
            "and a per-lane job env contract), so an off lane means every stage runs in-process and "
            "S1's watcher is dead code. See this test's docstring."
        )


def test_the_ray_address_names_a_service_the_chart_actually_creates() -> None:
    """`ray-lance-head` was the hardcoded default and does not exist in a KubeRay deployment.

    Measured 2026-08-15 from inside a pod: `ray-lance-head` fails DNS, `rask-ray-head-svc` answers
    `/api/version` with ray 2.56.1. The old value was the on-kind demo's raw head, and every mover
    would have submitted into a hostname that does not resolve — a failure that surfaces only when a
    trigger arrives.

    Derived from the release name rather than pinned, and pointing at the STABLE head service: the
    RayCluster KubeRay owns carries a generated suffix (`rask-ray-22nls`) that no chart can name and
    that changes on re-provision.

    RENDERED WITH THE LANE FORCED ON, because the default is now off (no Lance-capable cluster exists
    — see `test_the_ray_lane_is_OFF_until_a_LANCE_CAPABLE_cluster_exists`) and the address is only
    emitted when it is on. The property under test is what the value SAYS when it is present, so the
    fixture has to produce one; asserting against the default would silently test nothing.
    """
    docs = _rendered_docs("medallion.ray=true")
    services = {(doc.get("metadata") or {}).get("name") for doc in docs if doc.get("kind") == "Service"}
    addresses = {
        e.get("value")
        for doc in docs
        if doc.get("kind") == "Deployment"
        for c in doc["spec"]["template"]["spec"]["containers"]
        for e in (c.get("env") or [])
        if e.get("name") == "MEDALLION_RAY_ADDRESS"
    }
    assert addresses, "no mover declares MEDALLION_RAY_ADDRESS"
    for address in addresses:
        host = str(address).removeprefix("http://").split(":")[0]
        assert host.endswith("-ray-head-svc"), f"{address} does not name KubeRay's stable head service"
        assert "{{" not in str(address), "values.yaml is not templated — a {{ }} default ships literal braces"
        # The RayService creates it, so it is absent from the rendered docs when ray.enabled is off —
        # assert the SHAPE unconditionally and the existence only when the chart renders Ray at all.
        if any(str(s).endswith("-ray-head-svc") for s in services):
            assert host in services, f"{host} is not a Service this chart creates"


def test_every_mover_that_hosts_a_workflow_is_scoped_to_the_actor_state_store() -> None:
    """The general property the notifications test above only covers one instance of.

    S1 put a Dapr Workflow inside `mover.py`, so with the Ray lane on EVERY mover hosts a workflow —
    and each has its own `daprAppId` from `medallion.movers[]` (`bronze-to-silver`, `silver-to-gold`,
    …). There is no `medallion` app-id anywhere in the estate, and a hand-written scope entry for one
    was inert while looking entirely correct in review.

    THE FAILURE IS INVISIBLE IN THE SIDECAR LOG, which is why this is a test and not a convention.
    Measured live 2026-08-15 on an unscoped `bronze-to-silver`, in this order:

        "Actor state store not configured - actor hosting disabled, but invocation enabled"
        "Workflow engine started"

    The second line is the one an operator greps for and it is TRUE — the engine does start. Dispatch
    then fails on every delivery. After scoping, the first line is gone and the sidecar reports
    "Connected to placement service" instead.

    Asserted against the RENDERED movers rather than a hardcoded list, so adding a mover to
    `medallion.movers` cannot produce one that silently fails to dispatch.
    """
    docs = _rendered_docs("medallion.ray=true")
    stores = [
        doc
        for doc in docs
        if doc.get("kind") == "Component"
        and any(m.get("name") == "actorStateStore" and str(m.get("value")).lower() == "true" for m in (doc["spec"].get("metadata") or []))
    ]
    assert len(stores) == 1, f"expected exactly one actor state store, found {[d['metadata']['name'] for d in stores]}"
    scopes = set(stores[0].get("scopes") or [])

    # Filtered rather than `- {None}`: subtracting the sentinel does not narrow the ELEMENT type, so
    # the set stays `str | None` and `sorted` has nothing to compare. Narrow at the comprehension.
    movers = {
        app_id
        for doc in docs
        if doc.get("kind") == "Deployment" and "-to-" in ((doc.get("metadata") or {}).get("name") or "")
        if (app_id := (doc["spec"]["template"]["metadata"].get("annotations") or {}).get("dapr.io/app-id")) is not None
    }
    assert movers, "no movers rendered with medallion.ray=true — the fixture cannot prove anything"

    missing = sorted(movers - scopes)
    assert not missing, (
        f"these movers host a workflow but are not scoped to the actor state store: {missing}. "
        f"Their sidecars will log 'Workflow engine started' and disable actor hosting, so every "
        f"dispatch fails on a pod that reports itself healthy."
    )


# `test_the_kubelet_probes_the_inbox_on_a_path_the_service_actually_serves` lived here and is GONE,
# subsumed rather than deleted for tidiness: `tests/unit/test_probe_paths_are_served.py` asks the same
# question — is every path the chart probes one the app actually mounts — of every first-party app in
# the render, under that container's own chart env, instead of notifications alone. Keeping both would
# leave two mechanisms for one claim, and the weaker one was the reason the audit filed
# "the probe-path-is-actually-served gate covers exactly one of the fifteen apps".


def _notifications_cron_component(docs: list[dict]) -> dict:
    """The `bindings.cron` Component scoped to the notifications app-id."""
    for doc in docs:
        if doc.get("kind") == "Component" and (doc.get("spec") or {}).get("type") == "bindings.cron" and "notifications" in (doc.get("scopes") or []):
            return doc
    raise AssertionError("no bindings.cron Component is scoped to `notifications` — the /events reconciler would never tick")


def test_the_notifications_cron_binding_name_is_the_route_it_is_delivered_to() -> None:
    """The reconciler's Component name, its env, and the route the app serves are ONE string.

    Dapr delivers an input binding to `POST /<component name>` at the pod root. So a Component named
    one thing and a route mounted at another is a cron that fires into a 404 on every tick — with a
    healthy pod, a rendered Component, a running schedule, and nothing anywhere saying the deliveries
    are being dropped. The feed lane would simply never reconcile, which is indistinguishable from a
    feed that had nothing to reconcile.

    Guards all three corners at once, because any two of them can agree while the third drifts.

    The third corner runs in a SUBPROCESS, and that is load-bearing rather than tidy. The route path
    is bound at module import (`_binding = get_ingress_settings().binding_name`), so setting the env
    var in this process and calling `importlib.reload` proves nothing: reload re-executes the package
    `__init__` while `notifications.api.reconcile_cron` is already in `sys.modules`, so the pre-built
    router is re-included unchanged and the assertion silently degrades to "the chart default equals
    the code default". Worse, it then FAILS on a correct deployment the moment anyone edits
    `reconcileBindingName` — blaming the app for a chart change that a real pod, being a fresh
    process, honours. A subprocess IS that fresh process.
    """
    import os
    import subprocess
    import sys

    docs = _rendered_docs()
    component = _notifications_cron_component(docs)
    binding = component["metadata"]["name"]
    config = _fleet_config(docs)

    assert config.get("RASK_NOTIFICATIONS_BINDING_NAME") == binding, (
        f"the cron Component is named {binding!r} but the app is told {config.get('RASK_NOTIFICATIONS_BINDING_NAME')!r} — "
        "every tick would be delivered to a route the service does not serve"
    )

    probe = subprocess.run(
        [sys.executable, "-c", "import json,notifications; print(json.dumps(sorted(notifications.app.openapi()['paths'])))"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "RASK_NOTIFICATIONS_BINDING_NAME": binding, "RASK_API_PREFIX": config["RASK_API_PREFIX"]},
        cwd=REPO,
    )
    served = json.loads(probe.stdout)

    assert f"/{binding}" in served, f"the service serves no route at /{binding} — the cron Component ticks into a 404"


def test_the_notifications_reconciler_is_admitted_to_the_lineage_service_door() -> None:
    """The feed poll authenticates at lineage's SERVICE door, which is an explicit allowlist.

    `services.yaml` builds `LINEAGE_SERVICE_SUBJECTS` by scanning every service's own
    `env.RASK_LINEAGE_SERVICE_IDENTITY`, and the notifications service claims the same value. Omit the
    declaration and the reconciler 401s on every tick — a failure that reads as a credential problem
    while it is really the service never having been admitted at all.
    """
    docs = _rendered_docs()
    lineage = next(doc for doc in docs if doc.get("kind") == "Deployment" and (doc.get("metadata") or {}).get("name", "").endswith("-lineage"))
    env = {item["name"]: item.get("value") for item in lineage["spec"]["template"]["spec"]["containers"][0]["env"]}
    subjects = (env.get("LINEAGE_SERVICE_SUBJECTS") or "").split(",")

    container = _notifications_container(docs)
    claimed = {item["name"]: item.get("value") for item in container["env"]}.get("RASK_LINEAGE_SERVICE_IDENTITY")

    assert claimed, "the notifications service declares no RASK_LINEAGE_SERVICE_IDENTITY — its reconciler cannot reach the feed"
    assert claimed in subjects, f"lineage admits {subjects} but notifications claims {claimed!r} — every reconcile tick would 401"


def _lance_tracing_config(rendered: str) -> dict[str, Any] | None:
    """The one Dapr `Configuration` every sidecar references, or None if it did not render."""
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "Configuration" and doc.get("metadata", {}).get("name") == "lance-tracing":
            spec: dict[str, Any] = doc.get("spec") or {}
            return spec
    return None


def _retention_policy(spec: dict[str, Any]) -> dict[str, Any]:
    """The retention stanza, or an empty mapping. Narrowed rather than `or {}`-chained: the render is
    untyped YAML, so a `workflow` key holding something that is not a mapping must read as ABSENT here
    rather than raise an AttributeError that looks like a chart bug."""
    workflow = spec.get("workflow")
    if not isinstance(workflow, dict):
        return {}
    policy = workflow.get("stateRetentionPolicy")
    return policy if isinstance(policy, dict) else {}


def test_workflow_history_has_a_retention_policy() -> None:
    """Workflow history is append-only, and `stateRetentionPolicy` is the ONLY thing that collects it.

    Dapr's own CRD: "If not set, workflow instances will not be automatically purged." Nothing else in
    this estate purges them and nothing can — `DaprWorkflowClient` exposes `purge_workflow(id)` but no
    list-instances API, and `InMemoryRunStore` is documented as "deliberately NOT durable", so a sweep
    built on it would collect only what happened since the last pod start.

    Measured live 2026-08-10 before this landed: 1367 rows in `daprstate` for
    `dapr.internal.default.ingest.workflow`, `count(expiredate) = 0`, oldest row 2026-08-03 — every
    instance since the plane's first deploy, retained forever.
    """
    spec = _lance_tracing_config(_helm_template())

    assert spec is not None, "the Configuration every sidecar references did not render"
    policy = _retention_policy(spec)
    assert policy, "workflow history has no retention policy — it is retained forever"
    assert policy.get("completed"), "a COMPLETED run's history is never collected"
    assert policy.get("failed"), "a FAILED run's history is never collected"


def test_the_sidecars_non_telemetry_config_survives_telemetry_being_off() -> None:
    """THE REGRESSION THIS GUARDS, and it is the reason the Configuration is no longer otel-gated.

    A sidecar may reference exactly ONE `dapr.io/config`, so everything per-sidecar lives in one
    object — and that object used to render only when `lance.otelEnabled`. Retention is a DURABILITY
    concern, so hanging it there meant turning telemetry off silently returned the estate to unbounded
    workflow history. The gate now sits on the tracing stanza, which is the part that is about
    telemetry.

    RENAMED AND WIDENED rather than joined by a sibling, because retention was only the FIRST instance
    of the rule. Metric cardinality and api-logging are instances two and three: the sidecar's `:9090`
    exposition and its log stream both exist whether or not this estate ships a collector, so a bound
    that vanishes with telemetry is not a bound. A second test asserting the same rule from the same
    render would be a divergent answer to a settled question.

    ASSERTING THE LITERAL KEY NAMES IS THE WHOLE POINT, and it is the only gate there is. Helm does not
    request strict field validation, so `increasedCardinallity`, `recordErrorCode` or `obfuscateUrls`
    would render, apply CLEANLY, and be silently pruned by the API server — no error, no warning, no
    chart-visible signal. The symptom is "the setting had no effect", which is the same shape as the
    NullExporter defect this file already guards. Measured against the live k3s CRD: a strict-validating
    client rejects those three, and `--validate=ignore` (what Helm gets) stores the object with every
    typo dropped.

    `metric` (singular) is a real CRD key with a byte-identical schema and no stated precedence; daprd
    merges plural over singular field-by-field, so writing both would make that merge the only tiebreak.
    Plural only, asserted.
    """
    spec = _lance_tracing_config(_helm_template("observability.enabled=false"))

    assert spec is not None, "the Configuration vanished with telemetry — every sidecar's config reference now dangles"
    assert _retention_policy(spec), "retention was lost with telemetry"
    assert "tracing" not in spec, "tracing must still drop out when telemetry is off"

    metrics = spec.get("metrics")
    assert isinstance(metrics, dict), (
        f"no spec.metrics stanza with telemetry off (keys: {sorted(spec)}) — the sidecar still scrapes at :9090, so its cardinality bound must not be otel-gated"
    )
    assert "metric" not in spec, (
        "the legacy singular `metric:` key is also set — daprd merges the two field-by-field, so the manifest no longer says what is in force"
    )
    assert metrics.get("enabled") is True, "spec.metrics.enabled is REQUIRED by the CRD — without it the API server refuses the object outright"
    assert metrics.get("recordErrorCodes") is True, (
        "recordErrorCodes must sit under `metrics:`, NOT under `metrics.http:` — under http it is an unknown field and is pruned in silence"
    )

    http = metrics.get("http")
    assert isinstance(http, dict), "spec.metrics.http is missing"
    assert http.get("increasedCardinality") is False, (
        "increasedCardinality is not pinned false. At the upstream default the sidecar stamps the raw remainder of a "
        "service-invocation URL onto the `path` label — measured live: "
        'dapr_http_server_request_count{app_id="gateway",path="/v1.0/invoke/compute/method/api/ray/jobs"} — which is one '
        "series per table, namespace and project id, forever."
    )

    patterns = http.get("pathMatching")
    assert isinstance(patterns, list), "pathMatching must be a YAML sequence of strings; a map is refused by the API server"
    assert patterns, "pathMatching is empty, so every path collapses to the empty label"
    for pattern in patterns:
        assert not pattern.startswith("/v1.0/invoke/"), (
            f"{pattern!r} is a service-invocation pattern. daprd auto-registers an invoke twin for every OTHER entry here "
            "and registers the lot on a real http.ServeMux, which PANICS at startup on conflicting patterns. One "
            "Configuration serves every app-id, so that is the whole fleet crash-looping — triggered by a later one-line "
            "edit adding `/` or a bare `/{x...}` to this same list."
        )

    api_logging = (spec.get("logging") or {}).get("apiLogging")
    assert isinstance(api_logging, dict), "spec.logging.apiLogging is missing"
    assert api_logging.get("obfuscateURLs") is True, (
        "obfuscateURLs must be true and is INSEPARABLE from apiLogging.enabled: with it false daprd logs the raw "
        "`method + URL.Path`, and the actor URL carries base64url(<oidc sub>), which `decode_subject` reverses exactly. "
        "Enabling api logging without it would CREATE a subject exposure."
    )
    assert api_logging.get("omitHealthChecks") is True, "the kubelet polls /v1.0/healthz on every sidecar; without this the stream is mostly probe noise"


def test_every_sidecar_references_the_config_that_carries_retention() -> None:
    """A policy on an object nothing references governs nothing. Asserted with telemetry OFF, because
    that is the configuration in which the reference used to be omitted."""
    rendered = _helm_template("observability.enabled=false")

    assert 'dapr.io/config: "lance-tracing"' in rendered, "no sidecar references the retention config"
    assert _lance_tracing_config(rendered) is not None, "the referenced Configuration does not exist — a dangling reference"


def test_every_stream_has_its_retention_ASSERTED_not_merely_created() -> None:
    """§6 Q10 — retention is a fan-out correctness invariant, and creation alone cannot hold it.

    Both creation helpers in the stream Job are `if exists; then skip`, so a stream created ONCE with
    the wrong retention keeps it forever: the chart is right while the cluster is wrong, and nothing
    says so. That is not hypothetical — it was measured live on 2026-08-06, when INGEST was running
    `limits` while `queue.py` was built on `work`.

    What the wrong value costs is why this is a hard failure rather than a log line. `work` on a stream
    with several per-app durables means the FIRST ack deletes the message, so the apps steal deliveries
    from each other; `interest` empties the stream whenever no durable is attached, which is the exact
    state an ephemeral replay consumer depends on not happening. Neither surfaces as an error — both
    present as "some events did not arrive".

    Asserted on the rendered Job so a stream added without an assertion is caught HERE, not by a
    cascade that quietly under-runs.
    """
    job = _helm_template()
    marker = "assert_retention"

    assert marker in job, "the stream Job no longer asserts retention — a mis-created stream is undetectable again"
    # Every stream the Job creates must also be asserted. Parsed from the creation calls so a NEW
    # stream cannot be added with no assertion and still pass.
    created = {
        line.split()[1]
        for line in job.splitlines()
        if "_if_missing " in line and not line.strip().startswith("#") and len(line.split()) > 1 and line.split()[1].isupper()
    }
    asserted = {line.split()[1] for line in job.splitlines() if line.strip().startswith(f"{marker} ") and len(line.split()) > 1}
    asserted |= {"LINEAGE", "MEDALLION", "TRAINING", "DLQ", "CATALOG_CONTROL"} if "for stream in LINEAGE" in job else set()

    missing = created - asserted
    assert not missing, f"these streams are created but their retention is never asserted: {sorted(missing)}"


def test_inbound_retry_is_declared_on_the_COMPONENT_never_on_the_app() -> None:
    """§6 Q12(a) — the resiliency key that looks right and governs nothing.

    Dapr applies `targets.apps.<id>.retry` to SERVICE INVOCATION *out of* that app. Inbound pub/sub
    DELIVERY is governed only by `targets.components.<pubsub>.inbound.retry`. The two read almost
    identically in YAML, and putting a delivery policy under `apps` yields a CR that renders, validates
    and silently applies nothing to the thing it was written for — the cascade keeps whatever the
    component default is while the chart claims a policy.

    Asserted structurally on the rendered CR rather than by reading the file, so a future `apps` entry
    that grows an `inbound` block is caught here.
    """
    import yaml as _yaml

    rendered = _helm_template("dapr.resiliency.enabled=true")
    policies = [d for d in _yaml.safe_load_all(rendered) if d and d.get("kind") == "Resiliency"]

    assert policies, "no Resiliency CR rendered — this gate would pass vacuously"

    # COLLECTIVELY, not per-CR: the estate renders TWO — one for pub/sub (carrying `components`) and one
    # for service invocation (carrying `apps`). Asserting both properties of every CR fails the
    # invocation one for legitimately having no components, which is what the first version of this test
    # did.
    inbound_components: list[str] = []
    for cr in policies:
        targets = (cr.get("spec") or {}).get("targets") or {}
        for app_id, block in (targets.get("apps") or {}).items():
            assert "inbound" not in block, (
                f"targets.apps.{app_id} declares an `inbound` block. Inbound pub/sub delivery is governed "
                f"ONLY by targets.components.<pubsub>.inbound — this policy renders, validates, and applies "
                f"to nothing."
            )
        inbound_components += [name for name, block in (targets.get("components") or {}).items() if "inbound" in block]

    assert inbound_components, "no component anywhere declares an `inbound` policy — pub/sub delivery is ungoverned"


def test_the_app_log_filter_discriminates_by_SOURCE_not_by_POD() -> None:
    """The whole application log tier was being deleted, both copies, and nothing said so.

    THE INTENT. App pods export OTLP logs directly AND have their stdout tailed by the Collector's
    filelog receiver, so every app log arrived twice. `filter/drop_app_file_logs` exists to drop the
    file-tailed duplicate, keeping the OTLP original (which alone carries scope, severity and trace
    correlation).

    THE DEFECT. It dropped on `resource.attributes["lance.dev/logs"] == "otlp"` — a POD-level label
    extracted by `k8sattributes`. But k8sattributes associates records from BOTH receivers to that pod:
    the filelog rows by `k8s.pod.uid`, and the OTLP rows by `{from: connection}` (the app's own IP).
    One condition, both sources, same resource attribute — so the OTLP original was dropped alongside
    its duplicate and the app log tier reached GreptimeDB not at all.

    MEASURED, not theorised (2026-08-15, live k3s): `opentelemetry_logs` held 60.4M rows of which
    every one in the last 40 minutes had an EMPTY `scope_name` — i.e. filelog rows from pods that do
    NOT carry the label. Zero rows with a `medallion` scope; zero matching a stage failure this
    session's S1 drive had provably just emitted at ERROR.

    THE DISCRIMINATOR. The filelog receiver sets `include_file_path: true`, so its records — and only
    its records — carry `log.file.path`. That attribute is what tells the two sources apart; the pod
    label cannot, because both sources share the pod.
    """
    collector_conf = _collector_config(_rendered_docs("observability.enabled=true"))
    assert "filter/drop_app_file_logs" in (collector_conf.get("processors") or {}), (
        "no rendered ConfigMap carries the Collector config with `filter/drop_app_file_logs` — this gate "
        "would pass vacuously (the processor was renamed, or observability stopped rendering)"
    )

    conditions = collector_conf["processors"]["filter/drop_app_file_logs"]["logs"]["log_record"]
    assert conditions, "the filter has no conditions — it drops nothing, and every app log is duplicated"

    # The filelog receiver must keep announcing itself, or the discriminator below is a fiction.
    assert collector_conf["receivers"]["filelog"]["include_file_path"] is True, (
        "the filelog receiver no longer sets `include_file_path` — `log.file.path` is then absent from "
        "file-tailed records too, and the filter can no longer tell the two sources apart"
    )

    for condition in conditions:
        assert "log.file.path" in condition, (
            f"the app-log filter condition does not mention `log.file.path`:\n    {condition}\n\n"
            f"It therefore discriminates by POD, not by SOURCE. `k8sattributes` stamps "
            f"`lance.dev/logs=otlp` onto records from BOTH receivers (filelog by k8s.pod.uid, OTLP by "
            f"`{{from: connection}}`), so a pod-level condition drops the OTLP original as well as the "
            f"file-tailed duplicate — deleting the entire application log tier while the pipeline reports "
            f"healthy. Key on `log.file.path`, which only the filelog receiver sets."
        )
        assert "k8s.container.name" in condition and "daprd" in condition, (
            f"the app-log filter has no sidecar carve-out:\n    {condition}\n\n"
            "k8sattributes extracts the pod label `from: pod`, so it stamps `lance.dev/logs=otlp` onto the "
            "daprd container's records too — and Dapr registers a NullExporter for its own logs, so stdout is "
            "their ONLY copy. Measured 2026-08-23: an identical hot-reload ERROR line, emitted once a minute by "
            "every sidecar, was stored 0 times from all 10 labelled pods and 115 times from unlabelled ones."
        )
        assert "IsMatch(body," in condition, (
            f"the app-log filter has no body-shape guard:\n    {condition}\n\n"
            "A file-tailed line is a DUPLICATE only if a root-logger handler emitted it — that is where both "
            "`rask-stdout` (service_kit/__init__.py) and the OTel LoggingHandler live, and every such line starts "
            "with an ISO date. uvicorn's own records never reach root (uvicorn/config.py sets propagate=False on "
            '`uvicorn`), so "Application startup failed. Exiting.", the lifespan traceback and every ASGI '
            "exception have no OTLP twin at all. Without this clause they are deleted — and the filter is armed "
            "from pod admission, so the crash-loop window is exactly what it erases."
        )

    extract = collector_conf["processors"]["k8sattributes"]["extract"]["metadata"]
    assert "k8s.container.name" in extract, (
        'k8sattributes no longer extracts `k8s.container.name`, so the filter\'s `!= "daprd"` clause reads nil '
        "and is silently TRUE — the sidecar carve-out becomes a no-op and daprd goes dark again."
    )

    logs_processors = collector_conf["service"]["pipelines"]["logs"]["processors"]
    assert "filter/drop_app_file_logs" in logs_processors, (
        "the filter is defined but not in the logs pipeline — every app log is double-ingested, and NOTHING "
        "else in this suite reads the pipeline membership, so the hole would be invisible."
    )
    assert logs_processors.index("k8sattributes") < logs_processors.index("filter/drop_app_file_logs"), (
        "the filter runs before k8sattributes, so `lance.dev/logs` and `k8s.container.name` are not yet stamped and every condition evaluates against nil."
    )


def test_every_assert_retention_EXPECTS_a_string_nats_can_actually_emit() -> None:
    """The retention guard could never pass, and it blocks every `helm upgrade` of this chart.

    `assert_retention` reads the live value out of `nats stream info --json` with
    `sed -n 's/.*"retention":[ ]*"\\([a-z]*\\)".*/\\1/p'`, so `got` is whatever nats-server
    SERIALIZES — one of exactly `limits`, `interest`, `workqueue`. The INGEST call asked for `work`,
    which is not in that vocabulary, so `[ "$got" != "$want" ]` was true on every run once the stream
    existed. The Job then `exit 1`s, and with `--wait-for-jobs` the whole release upgrade fails.

    MEASURED 2026-08-15: revision 34 failed `context deadline exceeded` with
    `rask-nats-stream-r34` in CrashLoopBackOff, printing
    `!! STREAM INGEST HAS retention=workqueue, THE CHART INTENDS work.` — a stream that is correct
    (the chart creates it with `add_workqueue_if_missing`) being reported as drift by a guard whose
    expectation was a typo.

    `test_the_INGEST_stream_has_ONE_definition_and_the_chart_agrees_with_the_code` did not catch it
    because it checks the CREATION path's `--retention`, never the ASSERTION path's expected value.
    The two are separate strings and only one was pinned.
    """
    import re

    job = (REPO / "chart/templates/nats-stream-job.yaml").read_text(encoding="utf-8")

    # The vocabulary nats-server emits for `retention` in `stream info --json`. Anything else can only
    # ever mismatch, because the guard compares against this serialized form.
    NATS_RETENTIONS = {"limits", "interest", "workqueue"}

    calls = re.findall(r"^\s*assert_retention\s+(\S+)\s+(\S+)\s*$", job, re.MULTILINE)
    assert calls, "no `assert_retention` calls found — the guard was renamed or removed, and this gate now protects nothing"

    for stream, want in calls:
        assert want in NATS_RETENTIONS, (
            f"`assert_retention {stream} {want}` expects a retention nats-server never emits.\n"
            f"  got-side vocabulary: {sorted(NATS_RETENTIONS)}\n"
            f"  expected-side value: {want!r}\n\n"
            f'The comparison is `[ "$got" != "$want" ]` against the value parsed out of '
            f"`nats stream info --json`, so a value outside that set mismatches unconditionally, the Job "
            f"exits 1, and `helm upgrade --wait-for-jobs` fails the whole release."
        )


def test_every_database_the_age_chart_creates_ALSO_gets_the_age_EXTENSION() -> None:
    """`shared_preload_libraries = age` loads AGE into EVERY backend in the cluster, and its hook
    resolves `ag_catalog`. In a database where the extension was never created that schema does not
    exist, so **every DROP fails** — `ERROR: schema "ag_catalog" does not exist (SQLSTATE 3F000)`.

    MEASURED 2026-08-15 on the live estate. `CREATE TABLE` succeeded and `DROP TABLE` failed in both
    `openfga` and `daprstate`; the same round-trip passed in `lineage`, the one database that has the
    extension. `SHOW search_path` was `public` in all three, so this is NOT name resolution.

    WHAT IT BROKE. OpenFGA's migration 006 does `DROP INDEX CONCURRENTLY IF EXISTS
    idx_reverse_lookup_user`, so the store sat at schema version 5 and the migrate hook failed on
    EVERY `helm upgrade` — which is what failed release revision 34. The same trap is armed under
    Dapr's `daprstate`, which holds actor state and workflow history and whose schema Dapr migrates
    itself.

    THE PREVIOUS FIX TREATED THE SYMPTOM. `ALTER DATABASE ... SET search_path = public` is in the
    chart with the comment "so openfga migrations resolve objects in public, not a non-existent
    ag_catalog schema". Right symptom, wrong mechanism: the hook needs the schema to EXIST, not to be
    on the path. Keep both — the search_path line is still correct for its own reason.

    Removing `age` from `shared_preload_libraries` is not an option; AGE requires it.
    """
    text = (REPO / "chart/templates/age-postgres.yaml").read_text(encoding="utf-8")

    # The initdb scripts are ConfigMap keys run alphabetically by the postgres entrypoint.
    scripts = re.findall(r"^  (\d+-[\w.-]+\.sql): \|\n((?:^(?:    .*)?\n)+)", text, re.MULTILINE)
    assert scripts, "no initdb scripts found in the AGE ConfigMap — this gate would pass vacuously"

    creators = [(name, body) for name, body in scripts if "CREATE DATABASE" in body]
    assert creators, "no script creates a database — the chart changed shape and this gate no longer protects it"

    for name, body in creators:
        assert "CREATE EXTENSION IF NOT EXISTS age" in body, (
            f"{name} creates a database but never installs the AGE extension in it.\n\n"
            f"`shared_preload_libraries = age` loads AGE into every backend cluster-wide, and its hook "
            f"dereferences `ag_catalog`. Without the extension that schema does not exist in the new "
            f"database and EVERY DROP fails with SQLSTATE 3F000 — which is how OpenFGA's migration 006 "
            f"wedged at schema v5 and failed the release upgrade.\n\n"
            f"Add, after the CREATE DATABASE, a `\\c` into the new database followed by "
            f"`CREATE EXTENSION IF NOT EXISTS age;`."
        )


def test_every_database_the_age_chart_creates_is_ALSO_in_the_backup_dump_loop() -> None:
    """A database the chart creates and the backup job does not dump is data nobody knows is unprotected.

    `daprstate` was exactly that. `dapr-statestore.yaml` justifies putting Dapr's state on this Postgres
    with "already deployed, already backed up (backup-pg.yaml), already monitored" — and the dump loop
    named only `lineage` and `openfga`. The claim read as a decision that had been checked; nothing
    checked it. It holds the notifications plane's per-subject inbox READ state, which is the one thing
    the cron reconciler cannot rebuild from lineage's durable event feed.

    Derived from the initdb scripts rather than listed here, so the next database added to the chart is
    covered by this gate on the day it lands instead of the day someone remembers.
    """
    age_text = (REPO / "chart/templates/age-postgres.yaml").read_text(encoding="utf-8")
    values = (REPO / "chart/values.yaml").read_text(encoding="utf-8")
    backup = (REPO / "chart/templates/backup-pg.yaml").read_text(encoding="utf-8")

    # Every `.Values.age.<key>` a CREATE DATABASE names, e.g. `CREATE DATABASE "{{ .Values.age.stateDb }}"`.
    created = set(re.findall(r'CREATE DATABASE "\{\{ \.Values\.age\.(\w+) \}\}"', age_text))
    # …plus lineage, whose database is the one initdb makes itself rather than via CREATE DATABASE.
    created.add("lineageDb")
    assert len(created) >= 3, f"expected at least three databases, found {created} — the chart changed shape and this gate would pass vacuously"

    dump_loop = re.search(r"for db in ([^;]*); do", backup)
    assert dump_loop, "backup-pg.yaml no longer has a `for db in ...` loop — this gate cannot see what is dumped"
    dumped = set(re.findall(r"\.Values\.age\.(\w+)", dump_loop.group(1)))

    missing = created - dumped
    assert not missing, (
        f"chart/templates/age-postgres.yaml creates {sorted(created)} but backup-pg.yaml only dumps "
        f"{sorted(dumped)} — {sorted(missing)} would be lost with the volume and nothing would say so. "
        f"Add it to the `for db in ...` loop, and to docs/runbooks/RUNBOOK-restore.md, which is where "
        f"someone looks at 3am."
    )
    # …and the values keys must actually resolve, or the loop expands to an empty word and dumps nothing.
    for key in sorted(created):
        assert re.search(rf"^  {key}: \S+", values, re.MULTILINE), f"age.{key} is not set in values.yaml"


def test_the_backup_job_does_not_hide_a_failed_dump_behind_a_pipe() -> None:
    """`pg_dump | gzip` reports GZIP's status. The dump container runs `sh`, which in the AGE image is
    dash — `readlink -f /bin/sh` -> `/usr/bin/dash`, and `set -o pipefail` is NOT SUPPORTED there
    (both verified 2026-08-16 by running the real image). So `set -e` cannot see the dump fail.

    Measured in that image with a real `pg_dump` pointed at an unreachable host: the piped form printed
    no error, exited 0, and left a 20-byte gzip — which the upload container then shipped as that day's
    backup. A backup that fails loudly is an incident; a backup that fails silently is a data-loss event
    discovered during a restore.

    This gate does not mandate the mechanism, only that the naive pipe is not it.
    """
    backup = (REPO / "chart/templates/backup-pg.yaml").read_text(encoding="utf-8")
    # Comment lines are skipped — the shell block explains this very rule by quoting the bad form, and a
    # gate that fires on its own rationale teaches people to delete the rationale.
    piped = [
        stripped
        for line in backup.splitlines()
        if not (stripped := line.strip()).startswith("#") and "pg_dump" in stripped and "| gzip" in stripped and "||" not in stripped
    ]
    assert not piped, (
        "backup-pg.yaml pipes pg_dump straight into gzip:\n  "
        + "\n  ".join(piped)
        + "\n\nUnder dash (no pipefail) that reports only gzip's status, so a dump that dies mid-stream "
        "still exits 0 and a truncated file is uploaded as the backup. Capture pg_dump's own status — "
        "e.g. `{ pg_dump ... || touch /tmp/.dump-failed ; } | gzip > ...` then fail on the marker."
    )


def _reminders_without_a_failure_policy() -> list[str]:
    """Every `register_reminder(` call that does not state what happens when its callback FAILS.

    Dapr's default is to retry a failing reminder callback FOREVER — the docs are explicit that a
    reminder "will be retried" until the actor unregisters it or is deleted. A poison tick therefore
    pins the actor at the runtime's own pace, indefinitely.

    rask avoids that today by catching everything inside the callback and logging, which works and
    costs something real: the failure never reaches daprd, so it is absent from the sidecar's metrics
    and from any dead-letter surface. The estate is choosing a policy by hiding the failure rather
    than by declaring one.

    `ActorReminderFailurePolicy` (dapr 1.18.3, `dapr.actor.runtime.failure_policy`) is the declared
    form: `drop_policy()` discards a failed tick, `constant_policy(interval, max_retries)` bounds the
    retry. Either is a decision a reader can see; a bare `register_reminder` is one they cannot.
    """
    offenders: list[str] = []
    for py in SERVICES.rglob("*.py"):
        if "tests" in py.parts:
            continue
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            if "register_reminder(" not in line or "unregister_reminder(" in line:
                continue
            if "failure_policy" not in "\n".join(lines[i : i + 8]):
                offenders.append(f"{py.relative_to(REPO)}:{i + 1}")
    return offenders


def test_every_actor_reminder_declares_its_failure_policy() -> None:
    offenders = _reminders_without_a_failure_policy()
    assert not offenders, (
        f"these reminders never say what happens when their callback fails: {offenders}. Dapr's "
        "default retries forever, so the only thing standing between a poison tick and an infinite "
        "loop is a try/except inside the callback — which also hides the failure from daprd. Pass "
        "`failure_policy=ActorReminderFailurePolicy.drop_policy()` (or `constant_policy(...)`)."
    )


def test_notifications_stays_single_replica_while_its_single_flight_lock_is_process_local() -> None:
    """The reconcile tick's overlap guard is an `asyncio.Lock` — per PROCESS, not per estate.

    At one replica that is correct and cheap. At two, both pods tick, both read the same un-advanced
    cursor, and both walk the same rows: double the FGA and actor load exactly when lineage or the
    sidecar is already the slow thing. Nothing fails loudly; it just costs twice.

    The estate has already ruled on this exact shape, for the medallion movers — `values-prod.yaml`
    pins `moverReplicas: 1` with "the mover single-flight lock is PROCESS-LOCAL … Raise only after a
    cross-pod lock ships." That constraint is written down and enforced by a value. This one was only
    ever true by accident, so this test is the missing half.

    A Dapr actor with a fixed id WOULD give a cross-pod guard, and is deliberately not used: actors are
    turn-based, so they QUEUE. `reconcile_cron` argues for the opposite — "Skipping is the right answer
    rather than queueing: the work is not lost, it is what the pass already in flight is doing." An
    actor-based lock would have to re-implement skipping on top of queueing to preserve that.

    So: raise the replica count only together with a cross-pod guard that still SKIPS, and delete this
    test in the same commit.
    """
    cron = (SERVICES / "notifications" / "src" / "notifications" / "api" / "reconcile_cron.py").read_text()
    if "asyncio.Lock()" not in cron:
        return  # a cross-pod guard shipped; this test has done its job and should be deleted

    for profile in ("values.yaml", "values-prod.yaml"):
        values = yaml.safe_load((REPO / "chart" / profile).read_text()) or {}
        declared = ((values.get("services") or {}).get("notifications") or {}).get("replicas")
        assert declared in (None, 1), (
            f"chart/{profile} runs notifications at {declared} replicas while its reconcile overlap "
            "guard is a process-local asyncio.Lock, so every pod ticks and re-walks the same rows. "
            "Ship a cross-pod guard that SKIPS (not an actor, which queues) before raising this."
        )


def test_a_ONE_SHOT_reminder_never_uses_a_DROP_policy() -> None:
    """A dropped tick is recoverable only if another one is coming. On a one-shot, none is.

    `test_every_actor_reminder_declares_its_failure_policy` requires each `register_reminder` to
    STATE a policy. It cannot tell whether the policy fits the reminder, and the two legal answers
    have opposite consequences depending on `period`:

      * period > 0 (periodic) — `drop_policy()` is right. The tick is lost, the next one arrives on
        schedule, and the actor is not pinned retrying a poison payload.
      * period == 0 (one-shot) — `drop_policy()` means the callback runs AT MOST once and, if it
        fails, NEVER. Whatever the reminder was going to do is silently not done, forever.

    The estate has both, and the annotator already models the split correctly: `LEASE_REMINDER` is
    one-shot and takes `constant_policy(interval=10s, max_retries=6)`, because a dropped lease expiry
    leaves a task CLAIMED forever.

    THE CASE THIS CAUGHT. `DIGEST_REMINDER` was converted from periodic to one-shot while keeping
    `_DROP_THE_TICK`. `arm_digest` writes `DIGEST_KEY={"pending": True}` before registering, and
    `_digest_pending()` refuses to re-arm while that flag is set — so one failed tick leaves the
    window open, never re-fired and never re-armable. Silent and permanent, which is the exact
    failure class the reminder existed to prevent.
    """
    import ast

    offenders: list[str] = []
    for py in SERVICES.rglob("*.py"):
        if "tests" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a broken file is another gate's problem
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not ast.unparse(node.func).endswith("register_reminder"):
                continue
            if len(node.args) < 4:
                continue
            period = ast.unparse(node.args[3])
            # `timedelta(0)` / `timedelta(seconds=0)` — the two spellings of "fire once".
            if not re.fullmatch(r"timedelta\(\s*(?:seconds\s*=\s*)?0\s*\)", period):
                continue
            policy = next((ast.unparse(k.value) for k in node.keywords if k.arg == "failure_policy"), "")
            if "DROP" in policy.upper() or "drop_policy" in policy:
                offenders.append(f"{py.relative_to(REPO)}:{node.lineno} period={period} policy={policy}")

    assert not offenders, (
        "these reminders fire ONCE and drop the tick on failure, so a single failed callback means the "
        "work never happens and nothing retries it:\n  " + "\n  ".join(offenders) + "\n\n"
        "Use a retrying policy — `ActorReminderFailurePolicy.constant_policy(interval=..., max_retries=...)` "
        "— as `LEASE_REMINDER` does. `drop_policy()` is correct ONLY when `period > 0`, where the next "
        "scheduled tick is the retry."
    )


def test_every_RELEASE_touching_helm_call_goes_through_the_driver_seam() -> None:
    """Helm moved to the SQL storage driver, and forgetting it does not error — it LIES.

    The release lives in Postgres since 2026-08-15 (the Secret backend hit Kubernetes' hard 1 MiB
    limit). A `helm` invocation without `HELM_DRIVER=sql` reads the EMPTY Secret backend, concludes the
    release is absent, and — because the deploy targets use `upgrade --install` — installs over a live
    estate instead of upgrading it. There is no error to notice: it looks like a fresh cluster.

    So every release-touching call must go through ONE seam that sets the driver, the same way every
    image build goes through `scripts/dagger-image.sh`. `make k3s-up` used `$(HELM)` and would have
    been fixed by changing that variable alone; `kind-deploy` called BARE `helm upgrade --install` and
    would have kept the old behaviour silently. That is the bypass this gate exists to catch.

    Read-only subcommands are exempt and deliberately so — `helm template`, `lint`, `repo`,
    `dependency`, `show`, `version` never touch the release store, and routing them through a wrapper
    that REQUIRES a reachable database would break `make k3s-install` on a host with no cluster yet.
    """
    RELEASE_SUBCOMMANDS = ("upgrade", "install", "uninstall", "rollback", "history", "list", "status", "get")

    # SCRIPTS TOO, not just the Makefile. The first version read only the Makefile and passed while
    # `scripts/ray_e2e_stack.sh` called bare `helm upgrade --install` — the CI deploy path, i.e. the
    # one place a wrong-store install would be least noticed.
    sources = [MAKEFILE, *sorted((REPO / "scripts").glob("*.sh"))]

    offenders: list[str] = []
    for path in sources:
        if path.name == "helm.sh":  # the seam itself must call helm directly
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            body = line.split("#", 1)[0]  # a comment mentioning helm is not a call
            if not body.strip().startswith(("helm ", "@helm ", "-helm ")):
                continue
            rest = body.strip().split(None, 1)
            sub = rest[1].split()[0] if len(rest) > 1 else ""
            if sub in RELEASE_SUBCOMMANDS:
                offenders.append(f"{path.relative_to(REPO)}:{line_no}: {body.strip()[:90]}")

    assert not offenders, (
        "these Makefile recipes call `helm` DIRECTLY for a release operation, bypassing the driver "
        "seam:\n  " + "\n  ".join(offenders) + "\n\n"
        "Without HELM_DRIVER=sql they read the empty Secret backend, report the release as absent, and "
        "`upgrade --install` then RE-INSTALLS over a live estate with no error. Use $(HELM), which "
        "routes through scripts/helm.sh."
    )


def test_the_helm_seam_PICKS_the_store_that_holds_the_release() -> None:
    """The hazard is "helm succeeds against the wrong store", and BOTH directions are wrong.

    This gate first demanded the seam `exit 1` whenever it could not build a DSN. That was too strong
    and would have broken the two cases it never considered:

      * A FRESH INSTALL. `scripts/ray_e2e_stack.sh` installs the chart into an empty kind cluster,
        where the Postgres the SQL driver needs does not exist yet — because the chart creates it.
        Requiring the database makes the install that creates the database impossible.
      * A KIND CLUSTER AFTER INSTALL. It has an AGE pod now, but ITS release lives in the Secret
        store. "Use SQL whenever AGE exists" would report that release absent.

    So the seam probes whether the SQL store actually HOLDS anything and uses the store that does.
    Unreachable or empty means this release is not there, and the default driver is authoritative.
    The pass-through is announced on stderr, because the thing being prevented is a SILENT switch.
    """
    seam = REPO / "scripts/helm.sh"
    assert seam.exists(), "scripts/helm.sh is missing — $(HELM) points at a seam that does not exist"
    # CODE ONLY. Checking the raw text let a mutation pass: the probe string also appears in the
    # script's own explanatory comment, so removing the real one left the gate satisfied by prose.
    code = "\n".join(ln for ln in seam.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))

    assert "HELM_DRIVER" in code and "sql" in code, "the seam does not set HELM_DRIVER=sql"
    assert "helm list -aq" in code, (
        "the seam does not PROBE the SQL store. Without asking whether it holds a release, any rule it "
        "uses is a guess — and both obvious guesses ('always SQL', 'SQL whenever AGE exists') break a "
        "real environment. See this test's docstring."
    )
    assert ">&2" in code, (
        "the seam falls through without announcing it. A silent driver switch is exactly the failure "
        "this file exists to prevent; passing through must be visible."
    )


def test_the_ray_image_BAKES_every_job_script_the_medallion_entrypoints_name() -> None:
    """The Ray lane submitted an entrypoint the cluster's image did not contain, and every job died.

    `ray_submit` posts `settings.ray_entrypoint` — `python /home/ray/jobs/ray_stage_job.py` — to the
    Ray Jobs API. `.docker/ray-lance.dockerfile` bakes those scripts, but the chart's KubeRay cluster
    runs the **ray-cluster** image, which did not. Measured on the live cluster 2026-08-15:

        Running entrypoint for job ray-silver-...: python /home/ray/jobs/ray_stage_job.py
        python: can't open file '/home/ray/jobs/ray_stage_job.py': No such file or directory
        Job entrypoint command failed with exit code 2

    Every stage job failed this way, which is why the cascade's SUCCESS path had never once run — the
    fail path was exercised thoroughly and looked like a data problem.

    The trap is that the two halves live in different files and neither imports the other: a default in
    `medallion/core/config.py` and a COPY in a dockerfile. `medallion.ray` defaulting ON (2026-08-15)
    made that latent mismatch load-bearing, since the movers now point at the chart's unified cluster
    rather than the separate `ray-lance` demo the lane was first written against.
    """
    import re

    config = (REPO / "services/medallion/src/medallion/core/config.py").read_text(encoding="utf-8")
    scripts = set(re.findall(r"/home/ray/jobs/(\w+\.py)", config))
    assert scripts, "no /home/ray/jobs/*.py entrypoint defaults found — the config was restructured and this gate is now blind"

    dockerfile = (REPO / ".docker/ray-cluster.dockerfile").read_text(encoding="utf-8")
    missing = sorted(s for s in scripts if s not in dockerfile)

    assert not missing, (
        f"the chart's Ray cluster image does not bake: {missing}\n\n"
        f"`.docker/ray-cluster.dockerfile` is the image the KubeRay cluster actually runs, and "
        f"`medallion/core/config.py` names these under /home/ray/jobs/. A submitted job whose entrypoint "
        f"is absent fails with 'can't open file' and exit code 2 — the stage reports FAILED and the "
        f"cascade never completes, with nothing pointing at the image as the cause.\n\n"
        f"Add a COPY of scripts/<name> into /home/ray/jobs/, as .docker/ray-lance.dockerfile already does."
    )


def test_every_ray_job_script_is_BAKED_INTO_SOME_image() -> None:
    """A job script that no image bakes is a lane that can never run — and nothing said so.

    The sibling gate above only covers scripts named by an entrypoint DEFAULT in
    `medallion/core/config.py`. That was the whole surface while lanes were configured by env, and it
    stopped being the whole surface the moment a lane became a declared `TransformSpec`: an operator
    now names an entrypoint in a record, and the door can validate that the path is under the baked
    jobs directory but cannot know whether the image actually contains that file.

    Measured 2026-08-17: `scripts/ray_dummy_job.py` had existed for some time, referenced by neither
    dockerfile. It is the estate's own end-to-end probe — the one thing that proves the lane without a
    GPU — and it could not have run on any cluster. Nothing was red, because no config default named
    it and so the sibling gate never looked.

    Union across images on purpose, not per-image: `ray_lance_job.py` is the standalone demo that
    belongs to the separate ray-lance cluster, and requiring it in the KubeRay image would be wrong.
    What must never happen is a script baked NOWHERE.
    """
    scripts = {path.name for path in (REPO / "scripts").glob("ray_*_job.py")}
    assert scripts, "no scripts/ray_*_job.py found — the layout changed and this gate is now blind"

    baked = "\n".join((REPO / ".docker" / name).read_text(encoding="utf-8") for name in ("ray-cluster.dockerfile", "ray-lance.dockerfile"))
    orphans = sorted(name for name in scripts if name not in baked)

    assert not orphans, (
        f"these Ray job scripts are baked into NO image: {orphans}\n\n"
        f'A declared lane naming one dies with "can\'t open file" and exit code 2, and the stage '
        f"reports FAILED with nothing pointing at the image. Either COPY it into /home/ray/jobs/ in "
        f"the dockerfile whose cluster should run it, or delete the script — a job nothing can "
        f"execute is dead code, not a spare."
    )


def test_what_the_producer_PUBLISHES_is_what_a_mover_ACCEPTS() -> None:
    """A lane whose two halves disagree fails with a 200 OK and no log line anywhere.

    The producer stamps `bronzeDataset` / `bronzeNamespace` on the trigger it publishes to
    `bronzeTopic`. The mover subscribed to that topic compares the claim against its own
    `fromDataset` / `fromNamespace` and, on a mismatch, returns DROP — which is a SUCCESS ack. The
    wire looks healthy end to end:

        mover      POST /medallion-event  200 OK      <- the app accepted the delivery
        mover      POST /dlq-event        200 OK      <- and immediately dead-lettered it
        daprd      "DROP status returned from app while processing pub/sub event ..."

    and the mover's own log says NOTHING. Measured 2026-08-25, after the tiers were nested: the
    producer still published `bronze$events` while every mover had moved to `lakehouse$bronze$events`,
    so the cascade died at the first hop and the only evidence was one warning in a SIDECAR log.

    Pairing them here because they are rendered from different values by different templates and
    nothing else compares them: they are one contract written in two places.
    """
    import yaml as _yaml

    values = _yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    medallion = values.get("medallion") or {}
    producer = medallion.get("producer") or {}
    movers = medallion.get("movers") or []
    assert movers, "no movers declared — this gate is now blind"

    topic = producer.get("bronzeTopic")
    assert topic, "the producer declares no bronzeTopic, so nothing can consume its writes"

    consumers = [m for m in movers if m.get("subTopic") == topic]
    assert consumers, (
        f"the producer publishes to {topic!r} and no mover subscribes to it — the head writes bronze "
        f"and the cascade never starts, with every hop reporting success."
    )

    mismatched = []
    for mover in consumers:
        for producer_key, mover_key in (("bronzeDataset", "fromDataset"), ("bronzeNamespace", "fromNamespace")):
            want, got = producer.get(producer_key), mover.get(mover_key)
            if want != got:
                mismatched.append(f"  {mover.get('name')}: producer.{producer_key}={want!r} but mover.{mover_key}={got!r}")

    assert not mismatched, (
        f"the producer publishes a lane no mover on {topic!r} accepts:\n" + "\n".join(mismatched) + "\n\n"
        "The mover DROPs a trigger whose lane claim does not match its own, and DROP acks as success — "
        "so this fails with 200 OK on every hop and no error in the mover's log. Rename BOTH halves or "
        "neither."
    )

    # The media chain is the same contract through a different pair of values, and it drifted the same
    # way for the same reason: the URI was a literal in the template while the mover's had moved.
    media_ns = medallion.get("mediaBronzeNamespace")
    media_consumers = [m for m in movers if m.get("operation") == "derive_media"]
    for mover in media_consumers:
        assert media_ns == mover.get("fromNamespace"), (
            f"medallion.mediaBronzeNamespace={media_ns!r} but the media mover reads "
            f"{mover.get('fromNamespace')!r} — the head lands blobs where nothing is listening, and the "
            f"trigger is DROPped with a 200 OK."
        )
        # The DATASET is the half that actually gets compared, and it is the half the chart forgot:
        # MEDALLION_MEDIA_BRONZE_DATASET was rendered nowhere, so the head fell back to the flat code
        # default `bronze-media$objects` while the mover had moved. The template derives it as
        # `<namespace>$objects`; assert the same derivation rather than trusting it.
        assert f"{media_ns}$objects" == mover.get("fromDataset"), (
            f"the media head stamps dataset {media_ns}$objects but the mover accepts "
            f"{mover.get('fromDataset')!r} — a lane mismatch DROPs with a 200 OK and logs nothing."
        )


def test_a_medallion_NAMESPACE_can_actually_belong_to_a_warehouse() -> None:
    """With warehouses on, a flat tier belongs to nothing — and a PRE-qualified one gets qualified twice.

    `require_warehouse_scoped` refuses a top-level namespace that belongs to no warehouse, and every
    bucket a medallion tier could resolve to is reserved platform storage no warehouse may back (the
    catalog root in-app; anything in `medallion.buckets` by the chart, which appends that map into
    `LANCE_RESERVED_BUCKETS`). So a bare `bronze` is unownable. Two shapes escape that, and an estate
    must be in one of them:

    * **Project-qualified** (`medallion.projectsEnabled`) — `workflow.py::_qualified` prefixes
      `<project>-` at RUNTIME, so `bronze` becomes `acme-bronze`: still top-level, but owned by that
      project's warehouse. This is the shape `seed_estate.py` has always built.
    * **Nested** (`<parent>$bronze`) — the guard returns early for `len(segments) > 1`, so a child
      inherits its parent's warehouse and only the parent is bound.

    THE TRAP IS DOING BOTH. `_qualified` decides by `dataset.startswith(f"{project}-")`, and a nested
    name does not start with `<project>-` — it starts with `<parent>$`. Measured live 2026-08-25 on an
    estate whose project was `lakehouse` and whose tiers had been nested under a parent also called
    `lakehouse`:

        POST /v1/table/lakehouse-lakehouse$gold$catalog/create -> 403

    Every silver→gold hop failed on a table id that can never exist, and the mover reported only
    `medallion_stage_failed`. So with projects ON the declaration must stay UNQUALIFIED and unnested —
    the runtime owns the qualification, and pre-empting it doubles it.
    """
    import yaml as _yaml

    values = _yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    medallion = values.get("medallion") or {}
    warehouses_on = bool(((values.get("catalog") or {}).get("warehouses") or {}).get("enabled"))
    projects_on = bool(medallion.get("projectsEnabled"))
    delimiter = (values.get("catalog") or {}).get("delimiter") or "$"

    declared: list[str] = []
    head = (medallion.get("producer") or {}).get("bronzeNamespace")
    if head:
        declared.append(head)
    for mover in medallion.get("movers") or []:
        for key in ("fromNamespace", "toNamespace"):
            if mover.get(key) and mover[key] not in declared:
                declared.append(mover[key])
    assert declared, "no medallion namespaces declared — this gate is now blind"

    if projects_on:
        # The runtime qualifies. A declaration that is already nested (or already prefixed) is what
        # produces the doubled id above, so it is refused regardless of the warehouse setting.
        doubled = [ns for ns in declared if delimiter in ns]
        assert not doubled, (
            "medallion.projectsEnabled is true, so `_qualified` prefixes `<project>-` at runtime — but "
            "these namespaces are already nested, and a nested name does not start with `<project>-`, so "
            "it gets qualified ANYWAY:\n  " + "\n  ".join(doubled) + "\n\n"
            "The result is `<project>-<parent>$<tier>`, a table id nothing can create — every hop 403s and "
            "the mover logs only `medallion_stage_failed`. Declare the bare tier name and let the runtime "
            "qualify it."
        )
        return

    if not warehouses_on:
        return  # single-bucket: the shared root is the correct destination

    flat = [ns for ns in declared if delimiter not in ns]
    assert not flat, (
        "catalog.warehouses.enabled is true and medallion.projectsEnabled is false, so nothing qualifies "
        "these names and a top-level namespace must belong to a warehouse — but every bucket they could "
        "resolve to is reserved platform storage no warehouse may back:\n  " + "\n  ".join(flat) + "\n\n"
        "Turn on medallion.projectsEnabled (the runtime then owns `<project>-<tier>`, owned by that "
        f"project's warehouse), nest them under one bound parent ('<parent>{delimiter}<tier>'), or run "
        "single-bucket with catalog.warehouses.enabled=false."
    )


def test_ray_serve_is_actually_IMPORTABLE_from_the_root_lock() -> None:
    """Declaring `ray[serve]` is not the same as being able to import it, and the gap is upstream.

    The sibling gate below checks the DECLARATION. That would not have caught this, because the
    declaration was already correct: `ray[data,default,serve]` resolved 20 packages and the image
    still died at

        ModuleNotFoundError: No module named 'jinja2'.
        You can run `pip install "ray[serve]"` to install all Ray Serve dependencies.

    an error that names the extra it is already installing. Ray 2.58.0 declares jinja2 in NO extra
    (verified against ray-2.58.0.dist-info/METADATA: 69 Requires-Dist lines carry `extra == "serve"`,
    none of them jinja2) while `ray/serve/_private/haproxy.py:19` does `from jinja2 import Environment`
    at module load — so `import ray.serve` fails on a correctly-declared install.

    This asserts the thing that actually matters: the ROOT LOCK, which is what
    `.docker/ray-cluster.dockerfile` syncs, can import the module the KubeRay operator's dashboard
    query depends on. An upstream extra that silently loses a dependency is caught here rather than by
    a stalled cluster upgrade nobody is watching.
    """
    import importlib

    module = importlib.import_module("ray.serve")
    assert module is not None

    # The dashboard's Serve endpoint builds this model; importing the package alone does not prove the
    # schema path is intact, and it is the schema path the operator's GetServeDetails exercises.
    schema = importlib.import_module("ray.serve.schema")
    assert hasattr(schema, "ServeInstanceDetails"), "ray.serve.schema is missing ServeInstanceDetails — the operator's GetServeDetails would 500"


def test_the_ray_HEAD_image_can_answer_the_operator_about_SERVE() -> None:
    """A RayService that declares Serve apps needs ray[serve] on the HEAD, or upgrades never finish.

    This is a PLATFORM dependency wearing a workload's clothes, which is why it went unnoticed for two
    days. Serving an application needs ray[serve] in whatever image runs the replicas — and
    `runners/htr` correctly declares `ray[data,default,serve]`. But the KubeRay operator does not ask
    the replicas anything; it polls the HEAD's dashboard for `GetServeDetails`, and that endpoint is
    only implemented when ray[serve] is installed THERE.

    Measured 2026-08-25 on the live estate, erroring every ~17 minutes since 2026-08-23:

        Reconciler error ... failed to get Serve application statuses from the dashboard.
        err: GetServeDetails fail: 501 Not Implemented
             Serve dependencies are not installed. Please run `pip install "ray[serve]"`

    The consequence is not a failed Serve app — it is a stalled UPGRADE. KubeRay's zero-downtime path
    creates a pending RayCluster, waits for its Serve apps to report healthy, then switches and deletes
    the old one. A dashboard that cannot answer means "not yet healthy" forever, so the switch never
    happens and both clusters stay ready side by side (`BothActivePendingClustersExist`) — a silent,
    permanent doubling of the cluster's cost that nothing surfaces as an error.

    Derived, not hardcoded: the dockerfile names the package it installs, and the package names its
    extras, so this follows the same chain the build does.
    """
    import re

    import yaml as _yaml

    values = _yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    ray = values["ray"]
    if not (ray.get("serveApplications") or []):
        return  # no Serve applications declared -> the operator never polls for their status

    dockerfile_path = REPO / f".docker/{ray['image']['repository']}.dockerfile"
    assert dockerfile_path.exists(), f"ray.image.repository names no dockerfile: {dockerfile_path}"
    dockerfile = _uncommented(dockerfile_path.read_text(encoding="utf-8"))

    packages = re.findall(r"uv sync --package (\S+)", dockerfile)
    assert packages, f"{dockerfile_path.name} installs no workspace package — this gate cannot see what the head runs"

    missing = []
    for package in sorted(set(packages)):
        pyproject = REPO / "packages" / package / "pyproject.toml"
        if not pyproject.exists():
            continue
        # Comments stripped FIRST. The declaration's own rationale quotes the fix verbatim
        # (`pip install "ray[serve]"`), so a raw scan matches the prose and the gate passes exactly
        # when the file documents its own violation — the same trap the head-image gate hit.
        source = "\n".join("" if line.lstrip().startswith("#") else line for line in pyproject.read_text(encoding="utf-8").splitlines())
        declarations = re.findall(r'"ray\[([^\]]*)\][^"]*"', source)
        if not declarations:
            continue
        if not any("serve" in {extra.strip() for extra in d.split(",")} for d in declarations):
            missing.append(f"  packages/{package} declares ray[{declarations[0]}] — no `serve` extra")

    assert not missing, (
        "the chart declares Serve applications, but the head image's package does not install ray[serve]:\n"
        + "\n".join(missing)
        + "\n\nThe KubeRay operator polls the HEAD dashboard's GetServeDetails to decide whether a pending "
        "cluster is healthy. Without ray[serve] that answers 501, the zero-downtime switch never completes, "
        "and the estate keeps BOTH RayClusters running forever with nothing reporting an error."
    )


def test_the_deployed_ray_image_PROVIDES_every_serve_application_it_declares() -> None:
    """A declared Serve application names an import path the deployed image must actually contain.

    The sibling gates above cover JOB scripts — files under `/home/ray/jobs/` submitted to the Ray
    Jobs API. A Serve APPLICATION fails the same way for the same reason and was covered by nothing:
    `serveConfigV2` hands KubeRay an `import_path`, and if the image cannot import it the application
    never becomes healthy. The failure surfaces as a RayService stuck reconciling, not as anything
    naming the image.

    Measured 2026-08-25 on the shipped values: `ray.image.repository` is `ray-cluster` while
    `ray.serveApplications[0].importPath` is `runner.htrflow_service:htrflow_app`. `fd7dd7e0`
    (2026-08-18) deliberately emptied `ray-cluster` of every workload dependency — it builds
    `packages/ratch` from the ROOT lock and installs no runner at all — and moved the workload to
    `.docker/ray-htr.dockerfile`. The image half of that split landed; the chart was never pointed at
    the result, so the deployment declares an application the image it runs cannot import.

    This gate is the coupling made explicit: ONE RayService runs ONE image, so the image named here
    must satisfy every application declared here. That is also why a second workload is a second
    RayService rather than a second entry — an image containing both is the fattened shared image
    CLAUDE.md refuses.

    Workload-agnostic by construction: which runner provides which top-level module is read from the
    tree, never hardcoded, so this reads the same for audio, text, image or a modality nobody has
    written yet.
    """
    import yaml as _yaml

    values = _yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    ray = values["ray"]
    apps = ray.get("serveApplications") or []
    assert apps, "ray.serveApplications is empty — this gate is now blind; it exists to tie declared applications to the image that runs them"

    repository = ray["image"]["repository"]
    dockerfile_path = REPO / f".docker/{repository}.dockerfile"
    assert dockerfile_path.exists(), f"ray.image.repository is {repository!r} but .docker/{repository}.dockerfile does not exist"
    # `_uncommented`: `ray-cluster.dockerfile` explains in prose that it USED to build from
    # `runners/htr/uv.lock`, and a raw substring check reads that sentence as an install. A gate that
    # a comment can satisfy is a gate that passes exactly when the file documents its own violation.
    head_dockerfile = _uncommented(dockerfile_path.read_text(encoding="utf-8"))
    # The parametrized per-workload image builds ANY runner from `ARG RUNNER`, so its dockerfile names
    # none. An application that declares its own `image` is satisfied by it for whichever runner the
    # tag was built from — the pairing this gate can check statically is that the module EXISTS.
    runner_image = REPO / ".docker/ray-runner.dockerfile"

    # Which runner ships which top-level module, read from the tree so no workload is named here.
    provider = {src.name: src.parent.parent.name for src in (REPO / "runners").glob("*/src/*") if src.is_dir()}

    unsatisfied = []
    for app in apps:
        module = app["importPath"].split(":")[0].split(".")[0]
        runner = provider.get(module)
        if runner is None:
            unsatisfied.append(f"  {app['name']}: imports {module!r}, which no runners/*/src/ provides")
            continue
        if app.get("image"):
            # Declared its own baked image (rendered as runtime_env.image_uri). It must be buildable:
            # the parametrized dockerfile is the only thing that builds one, and a runner without its
            # own lock cannot be sealed into an image at all (`uv sync --locked` is the seal).
            if not runner_image.exists():
                unsatisfied.append(f"  {app['name']}: declares image {app['image']!r} but .docker/ray-runner.dockerfile does not exist to build it")
            elif not (REPO / "runners" / runner / "uv.lock").exists():
                unsatisfied.append(f"  {app['name']}: declares an image, but runners/{runner} ships no uv.lock — there is no sealed environment to bake")
        elif f"runners/{runner}" not in head_dockerfile:
            unsatisfied.append(
                f"  {app['name']}: imports {module!r} (from runners/{runner}), and declares no image of its own, "
                f"so it falls back to the head image {repository!r} — which does not install that runner"
            )

    assert not unsatisfied, (
        "a declared Serve application names an import path no image it can run provides:\n"
        + "\n".join(unsatisfied)
        + "\n\nGive the application its own baked image — `serveApplications[].image`, rendered as "
        "runtime_env.image_uri and built by\n"
        "    scripts/dagger-image.sh --runner <runner> --tag ray-<runner>:<tag>\n"
        "from the parametrized .docker/ray-runner.dockerfile. Do NOT install a second runner into the "
        "head image to satisfy it — that is the fattened shared image CLAUDE.md refuses, and it is what "
        "fd7dd7e0 removed."
    )


def test_stage_run_is_a_MONITOR_and_uses_continue_as_new() -> None:
    """A poll loop inside one instance grows history without bound; `continue_as_new` resets it.

    Dapr's Monitor pattern is explicit: "Rather than writing infinite while-loops (which is an
    anti-pattern), Dapr Workflow exposes a continue-as-new API." `stage_run` polls a Ray job, which is
    exactly that pattern.

    The bounded `for attempt in range(spec.max_polls)` it used was not the literal anti-pattern — but
    the bound WAS the history bound, and it was set at `MAX_POLLS = 2880` polls x 30 s = 24 hours.
    Every turn appended a timer and an activity plus their results, so one instance could accumulate
    ~5,760 history events, all of it replayed from the start on every continuation. The ceiling on how
    long a stage could take was therefore the ceiling on how much history one instance could carry —
    two unrelated things welded together.

    With `continue_as_new` the loop can be indefinite because each turn starts with empty history, and
    `max_polls` goes back to meaning only "how long are we willing to wait".

    THE CARRIED STATE IS THE RISK. `continue_as_new` restarts the workflow immediately and DISCARDS
    any task started but not awaited, so the submission id and the poll count must ride the new input
    or the next turn re-submits the job and counts from zero. That is what
    `test_stage_run_does_not_RESUBMIT_after_continue_as_new` pins.
    """
    import ast

    body = (REPO / "services/medallion/src/medallion/workflow.py").read_text(encoding="utf-8")
    tree = ast.parse(body)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "stage_run")
    src = ast.unparse(fn)

    assert "continue_as_new" in src, (
        "stage_run does not call ctx.continue_as_new. It is a Monitor — it polls an external job on a "
        "timer — and Dapr's guidance for that pattern is continue-as-new precisely so history does not "
        "grow with the wait."
    )
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For | ast.While)]
    assert not loops, (
        f"stage_run still contains {len(loops)} loop(s). With continue_as_new each instance performs ONE "
        f"poll and hands the rest to the next turn; a surviving loop means history still accumulates "
        f"inside a single instance, which is the thing being fixed."
    )


def test_stage_run_does_not_RESUBMIT_after_continue_as_new() -> None:
    """The submission id must survive the turn, or every poll starts a second Ray job.

    `continue_as_new` gives the next turn a fresh history — which means the next turn has no memory
    that `submit_stage` already ran. If the spec does not carry the submission id forward, the guard
    `if not submission_id` is the only thing between this workflow and submitting the same stage job
    once per poll interval, forever, each one overwriting the same output dataset.
    """
    from medallion.workflow import StageJobSpec

    fields = StageJobSpec.model_fields
    assert "submission_id" in fields, "StageJobSpec cannot carry the submission id across a continue_as_new turn"
    assert "polls_done" in fields, "StageJobSpec cannot carry the poll count across a turn — the ceiling would never be reached"
    assert fields["submission_id"].default is None, "submission_id must default to None so the FIRST turn submits"
    assert fields["polls_done"].default == 0, "polls_done must default to 0"


def test_k3s_up_does_not_REWRITE_the_image_mode_of_a_live_release() -> None:
    """`make k3s-up` hardcoded an image mode that contradicts the estate it deploys to.

    It passed `--set image.localImages=true`, which renders every image as a BARE `<name>:<tag>` the
    kubelet must already hold. The live release runs `localImages: false` with
    `repository: localhost:5000` — the dev registry — so running the documented deploy command would
    have rewritten the whole fleet to names that resolve to Docker Hub and ImagePullBackOff.

    That is the #135 failure exactly, and the chart's own guard says so: "A bare name resolves to
    Docker Hub and will ImagePullBackOff." It has been measured twice, once costing 22
    `kubectl set image` calls to recover.

    The fix is not to flip the flag — a side-loaded estate genuinely wants `localImages=true`. It is
    to stop ASSUMING: capture what the release already uses and hand it back to helm, so an existing
    estate keeps its own answer and a fresh one still gets the side-load default.
    """
    recipe = MAKEFILE.read_text(encoding="utf-8")
    start = recipe.index("k3s-up:")
    body = recipe[start : recipe.index("\nseed-corpus:", start)]

    assert "get values" in body, (
        "make k3s-up does not read the live release's values before upgrading. Without that it imposes "
        "an image mode on an estate that may already use a different one — the #135 fleet-wide "
        "ImagePullBackOff. See this test's docstring."
    )
    assert "--set image.localImages=true \\\n" not in body, (
        "make k3s-up still hardcodes image.localImages=true. That must be a FALLBACK for a release that does not exist yet, never an override of one that does."
    )


def test_the_control_emitter_has_exactly_one_implementation() -> None:
    """THE THIRD COPY IS THE ONE THAT DOESN'T GET WRITTEN.

    `control_emit` was duplicated between the catalog and maintenance — same Protocol, same Noop/Dapr
    pair, same fail-open posture, differing only in an OTel meter name. The duplicate was deliberate and
    its docstring says why: *"maintenance may not import the catalog"*
    (:func:`test_declared_dependencies`), *"the shared code is the event MODEL"*.

    That reasoning justifies not importing the CATALOG. It never justified a second implementation —
    `service_kit` already owns the wire model, and it is importable by every service by construction. The
    annotator emitting `task_assigned` would have been the third copy, and three copies of a fail-open
    publish path is three places for the swallow-and-count discipline to drift apart.

    Guarded rather than merely fixed: the next producer must reach for the shared module, and a fourth
    copy fails here instead of passing review.
    """
    shared = REPO / "packages/service-kit/src/service_kit/control_emit.py"
    assert shared.is_file(), "the ONE control emitter belongs in service_kit, beside the event model it publishes"

    strays = sorted(path.relative_to(REPO).as_posix() for path in REPO.glob("services/*/src/*/**/control_emit.py") if "NoopControlEmitter" in path.read_text())
    assert strays == [], f"control_emit re-implemented per service instead of imported from service_kit: {strays}"


def test_every_service_that_raises_its_loggers_is_ON_the_allowlist() -> None:
    """A LOG TIER THAT SILENTLY DELETES ITSELF — the rename that took the sweep's only report with it.

    `configure_app_logging` raises a fixed tuple of package loggers to INFO so their records reach the
    OTLP handler. Everything else inherits root's WARNING, so a package MISSING from that tuple keeps
    emitting `log.info(...)` into nothing — no stdout, no GreptimeDB, no error anywhere.

    `services/compaction` was renamed to `services/maintenance`, and the allowlist was not. The entry
    `"compaction"` went on naming a package that no longer exists while `maintenance` was never added,
    so `log.info("maintenance_sweep", extra=summary)` — the sweep's ONLY report of what it did — reached
    nobody for the whole life of the renamed service. MEASURED live 2026-08-16: the sweep had run on a
    120s cron across ~40 pod generations and not one summary line existed to read, which is why "has it
    ever reclaimed anything?" could not be answered from the logs at all.

    Both halves are guarded, because either alone would have let this through: a caller absent from the
    allowlist is muted, and an allowlist entry naming no package is the dead rename that hid it.
    """
    obs = REPO / "packages/service-kit/src/service_kit/obs.py"
    listed = set(re.findall(r'^\s*"([a-z_]+)",', obs.read_text(), re.MULTILINE))
    assert listed, "could not parse _APP_LOGGERS out of obs.py — the guard must fail loudly, not vacuously"

    # The package is the directory directly under `src` — parts = (services, <svc>, src, <package>, ...).
    callers = {path.relative_to(REPO).parts[3] for path in REPO.glob("services/*/src/*/**/*.py") if "configure_app_logging(" in path.read_text()}
    assert callers, "no caller of configure_app_logging found — the parse is wrong, not the estate"

    muted = sorted(callers - listed)
    assert muted == [], f"these services raise their loggers but are not on the allowlist, so their INFO records reach nobody: {muted}"

    packages = {path.name for path in REPO.glob("services/*/src/*") if path.is_dir()}
    packages |= {path.name for path in REPO.glob("packages/*/src/*") if path.is_dir()}
    dead = sorted(name for name in listed - packages if name not in {"common", "ratch"})
    assert dead == [], f"_APP_LOGGERS names packages that do not exist (a rename left the old name behind): {dead}"


def test_every_lifecycle_door_that_clears_PROTECTION_also_handles_the_POLICY_record() -> None:
    """TWO CONTROL-ROOT RECORDS, ONE LIFECYCLE — and only one of them was being cleaned up.

    Protection and maintenance-policy records are siblings: both live on the control root, both are keyed
    by `kind` + canonical id, both are written by their own endpoint and both are meaningless once their
    object is destroyed. The drop and cascade doors cleared protection and said why — *"the record dies
    with the object — a reused id must not inherit protection nobody set on it"* — and simply never
    learned about the policy. `delete_policy` was reachable ONLY from the three explicit policy-delete
    endpoints, so every destroyed table and every cascaded subtree leaked one.

    The leak is not inert, which is what makes it worth a guard rather than a cleanup. The sweep
    discovers datasets by walking storage for a `_versions/` marker, NOT by reading the registry, so a
    later table created at the same canonical id is swept under a retention window, fragment sizing and
    cleanup toggles that nobody set on it — silently, and forever.

    Anchored on `clear_protection` deliberately: it marks exactly the doors that have already decided
    "this object is going away", so the next record type added to the control root is dragged into the
    same decision instead of quietly repeating this.
    """
    doors = {
        "services/catalog/src/catalog/api/v1/endpoints/tables.py",
        "services/catalog/src/catalog/api/v1/endpoints/namespaces.py",
    }
    # Doors that clear protection but deliberately do NOT touch the policy, each with a recorded reason.
    exempt = {
        # Not lifecycle at all — the protection set/clear endpoints themselves.
        "set_table_protection",
        "set_namespace_protection",
        # deregister keeps the BYTES on purpose (external data) and the sweep still discovers that
        # dataset by its `_versions/` marker, so its policy still governs something real. Protection's
        # jurisdiction is governance and ends here; the policy's is the dataset on storage, and does not.
        "deregister_table",
    }

    offenders: list[str] = []
    for rel in sorted(doors):
        tree = ast.parse((REPO / rel).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            body = ast.dump(node)
            if "clear_protection" not in body or node.name in exempt:
                continue
            if "maintenance_policies" not in body:
                offenders.append(f"{rel}::{node.name}")

    assert offenders == [], (
        "these doors destroy an object and clear its protection record but leave its maintenance policy "
        f"behind, where a later object reusing the id inherits it: {offenders}"
    )


def test_the_sweep_lock_is_only_correct_while_maintenance_CANNOT_scale() -> None:
    """AN IN-PROCESS LOCK IS A CLUSTER LOCK ONLY AT ONE REPLICA — and nothing tied the two together.

    `routes.py` guards the sweep with a module-level `asyncio.Lock`, because a slow sweep can outlast the
    120s cron and a second concurrent pass would race `compact_files()` / `cleanup_old_versions()` on the
    same datasets — concurrent commits, and a GC deleting versions the other pass is still reading.

    That lock is process-local. It is a CLUSTER-wide guarantee only because
    `chart/templates/maintenance.yaml` hardcodes `replicas: 1` as a literal, with no values key to
    override — scaling is unreachable except by a template edit or a `kubectl scale`. So the invariant
    holds by accident of an unparameterised template, and the accident is undocumented anywhere the
    person adding `maintenance.replicas` would look.

    The routes.py comment already flags this and points at a plan that was never landed; it also cited a
    `compactionReplicas` values key that EXISTS NOWHERE, so the citation meant to reassure a reader was
    itself drift. This binds the two facts mechanically instead: while the lock is an `asyncio.Lock`, the
    deployment must not be scalable. Replace it with a distributed lock and this guard steps aside.
    """
    routes = (REPO / "services/maintenance/src/maintenance/api/routes.py").read_text()
    if "asyncio.Lock()" not in routes:
        pytest.skip("the sweep no longer uses an in-process lock — a distributed one may scale freely")

    template = (REPO / "chart/templates/maintenance.yaml").read_text()
    replicas = [line.strip() for line in template.splitlines() if re.match(r"^\s*replicas:", line)]
    assert replicas, "the maintenance Deployment declares no replica count at all — one concurrent sweep is not guaranteed"
    assert len(replicas) == 1, f"more than one replicas declaration to reason about: {replicas}"

    assert replicas[0] == "replicas: 1", (
        f"maintenance declares {replicas[0]!r}. The sweep's single-flight is a module-level asyncio.Lock, "
        "which guards ONE process — a second replica runs a second concurrent sweep that races "
        "compact_files()/cleanup_old_versions() on the same datasets. Make the lock distributed before "
        "making the deployment scalable."
    )


def test_every_perses_dashboard_is_declared_ONCE_and_is_valid_json() -> None:
    """A REPEATED ConfigMap KEY silently discards a dashboard — YAML keeps the last one and says nothing.

    `notifications.json` was declared twice, byte-identical, so the file carried 497 lines to provision
    six documents. Harmless only because the duplicate agreed with the original: had one copy been
    edited, Perses would have served the OTHER, and every reading of the template would have shown the
    change that was not deployed. Neither Helm nor `kubectl apply` reports a duplicate key.

    The JSON check rides along because the same failure shape applies one layer down: this template is
    JSON embedded in YAML embedded in Go templating, and a dashboard that does not parse is not a
    render error — Perses simply skips it at provisioning time, on a pod nobody is watching, leaving a
    dashboard that "exists" in the chart and nowhere else.
    """
    raw = (REPO / "chart/templates/perses-dashboards.yaml").read_text()

    keys = re.findall(r"^  ([A-Za-z0-9_.-]+\.json): \|", raw, re.MULTILINE)
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert dupes == [], f"these Perses documents are declared more than once, so all but the last are silently dropped: {dupes}"

    # Render it, so the assertion covers what SHIPS rather than the pre-template text.
    docs = [d for d in yaml.safe_load_all(_helm_template("observability.enabled=true")) if d]
    configmaps = [d for d in docs if d.get("kind") == "ConfigMap" and "perses-dashboards" in d.get("metadata", {}).get("name", "")]
    assert len(configmaps) == 1, f"expected exactly one perses-dashboards ConfigMap, got {len(configmaps)}"
    data = configmaps[0].get("data", {})
    assert len(data) == len(set(keys)), f"rendered {len(data)} documents from {len(set(keys))} declared keys"
    for name, document in data.items():
        try:
            json.loads(document)
        except json.JSONDecodeError as exc:  # noqa: PERF203 — one message per offender is the point
            pytest.fail(f"{name} is not valid JSON, so Perses will skip it at provisioning time: {exc}")


#: Alert group -> the module whose OTel instruments its rules may reference. A first-party group
#: asserts against its own service; the three omitted groups reference metrics no rask instrument
#: creates and are listed with the reason rather than silently skipped.
#: A group's rules may query instruments from MORE THAN ONE module — the outbox ones live in the shared
#: `service_kit` package, not in the service's own metrics.py. A single-path mapping made those invisible:
#: the gate derives its match pattern from the prefixes it FINDS, so a metric whose prefix is absent from
#: the source is not merely unchecked, it is unmatchable — which is exactly how `outbox_oldest_age`
#: survived as a phantom series name for the life of the rule.
_ALERT_GROUP_SOURCES: dict[str, tuple[str, ...]] = {
    "lance-lineage": (
        "services/lineage/src/lineage/core/metrics.py",
        "packages/service-kit/src/service_kit/lakehouse/outbox_metrics.py",
    ),
    "lance-medallion": ("services/medallion/src/medallion/core/metrics.py",),
    "lance-notifications": ("services/notifications/src/notifications/api/metrics.py",),
    "lance-maintenance": ("services/maintenance/src/maintenance/core/metrics.py",),
    # Both added 2026-08-26. Their instruments existed and NO rule read them — `ingest/metrics.py`
    # said so in its own docstring — which the forward gate cannot catch and
    # `test_every_FIRST_PARTY_INSTRUMENT_is_read_by_some_alert_rule` now does.
    "lance-ingest": ("services/ingest/src/ingest/metrics.py",),
    "lance-flows": ("services/flows/src/flows/metrics.py",),
    # Both moved OUT of `_THIRD_PARTY_ALERT_GROUPS` 2026-08-27 (open_fastapi-audit). Neither entry was
    # true, and being there exempted the group from the phantom gate entirely.
    #
    # `lance-catalog` claimed the catalog "ships no metrics.py — its rules ride the shared HTTP server
    # metrics". Both halves false: `catalog.writes.shed` is created by `_meter.create_counter` in
    # `api/load_shed.py` (not a file called metrics.py, which is what the claim was built on), and no
    # lance-catalog rule references any `http_server_*` series.
    #
    # `ray` was not named by the audit at all — the classification gate below found it. It claimed
    # "Ray's own metrics, exported by the Ray dashboard's Prometheus endpoint", and two of its five
    # rules query `ray_control_probes_total` / `ray_control_jobs_known`, which ray-kit instruments.
    "lance-catalog": (
        "services/catalog/src/catalog/api/load_shed.py",
        "services/catalog/src/catalog/core/lineage_emit.py",
    ),
    "ray": ("packages/ray-kit/src/ray_kit/metrics.py",),
}

#: Series namespaces the estate QUERIES but does not emit, with who does emit them.
#:
#: `ray` is genuinely MIXED, which neither classification could express: ray-kit instruments
#: `ray.control.*` while Ray's own exporter produces `ray_node_*`, `ray_serve_*`, `ray_data_*`,
#: `ray_tasks`, `ray_resources` and more. The alternative was splitting the alert group in two, and
#: that renames an operator-facing group — runbooks and dashboards reference these names — to solve a
#: modelling problem. So the exemption is by NAMESPACE, not by exempting a whole group from the gate.
#:
#: Enumerating Ray's series one by one was the first attempt and is the wrong shape: Ray's set is large
#: and evolves with the version, so the list would rot into a second false claim of exactly the kind
#: this finding is about.
_EXTERNAL_SERIES_PREFIXES: dict[str, str] = {
    "ray_": "Ray's own dashboard/Prometheus exporter",
}

#: The carve-out that keeps the gate sharp. Without it the `ray_` rule would also excuse a TYPO in
#: ray-kit's own series (`ray_control_probes_totl`), which is the failure the gate exists for.
_OWNED_DESPITE_EXTERNAL_PREFIX: tuple[str, ...] = ("ray_control_",)


def _is_external_series(name: str) -> bool:
    """True when a series is emitted by something outside this repo."""
    if name.startswith(_OWNED_DESPITE_EXTERNAL_PREFIX):
        return False
    return name.startswith(tuple(_EXTERNAL_SERIES_PREFIXES))


#: UCUM unit -> the suffix the OTLP->Prometheus exporter APPENDS to the series name. A unit in curly
#: braces is a dimensionless annotation (`{event}`, `{run}`) and is dropped, which is why counters keep
#: their bare name. This mapping is the rule `outbox_oldest_age` violated: the instrument declares
#: `unit="s"`, so the real series is `outbox_oldest_age_seconds` — verified against the live store's
#: information_schema — and a rule naming the bare form can never fire.
_UCUM_SUFFIX = {"s": "seconds", "ms": "milliseconds", "By": "bytes"}

#: Groups whose rules query series the estate does not instrument, with why. An entry is a claim.
_THIRD_PARTY_ALERT_GROUPS = {
    "lance-infra": "infrastructure series (NATS, CloudNativePG, RustFS) exported by their own operators",
    "lance-http": (
        "the FastAPI instrumentor's own RED series (`http_server_duration_milliseconds_*`), emitted "
        "automatically by `service_kit.setup_otel` for all fourteen apps — no rask instrument creates "
        "them, which is the same reason `lance-catalog` is here. The `_milliseconds` suffix is the "
        "OTLP->Prometheus unit convention and is load-bearing: verified against the live store, since "
        "a rule naming the bare form matches nothing and can never fire"
    ),
    "dapr-control-plane": "Dapr's own control-plane metrics, emitted by the sidecar injector and placement service",
    "lance-observability": (
        "the telemetry backend's own health, from the `greptimedb` scrape job in otel-collector.yaml. "
        "`up` and `process_start_time_seconds` are synthesised by the SCRAPER and `process_resident_memory_bytes` "
        "comes from GreptimeDB's process exporter — no rask instrument creates any of them, and none should: "
        "a first-party metric about the store would be written INTO the store it is reporting on"
    ),
}

#: Alert group -> the OTel Collector receiver id that DECLARES its series. A group here is neither
#: first-party (no rask instrument writes it) nor third-party (nobody else's exporter writes it
#: either): the metric is declared IN THIS REPO, by a receiver in chart/templates/otel-collector.yaml.
#:
#: `sqlquery/daprstate` is the case that forced the third class into existence. The Dapr sidecar
#: exports NO workflow, actor or state-store metric at all — enumerated live on 2026-08-26 against the
#: `ingest` sidecar, the whole surface is dapr_error_code_total, dapr_grpc_io_*, dapr_http_*,
#: dapr_runtime_component_{init_total,loaded} and go_*, and `dapr_runtime_workflow_*` (which this
#: file's own DaprConsumerWedge note already calls unreliable here) is absent outright. So there is no
#: scrape target to point at: the only way to alert on workflow-history retention is to measure the
#: state store directly, which the Collector does with a SQL query rather than a new service.
#:
#: It gets a binding for exactly the reason the other two classes have one. A rule naming a metric the
#: receiver does not declare never fires, and never firing is what a healthy estate looks like.
_COLLECTOR_ALERT_GROUPS: dict[str, str] = {
    "dapr-workflow-state": "sqlquery/daprstate",
}


def test_every_ALERT_GROUP_is_either_first_party_or_declared_third_party() -> None:
    """The gate below covered ONE group of eight, and a scope that narrow is its own hazard.

    A rule in an unchecked group could name `medallion_stage_deniedTYPO_total` and pass
    `promtool check rules`, `promtool test rules` and every chart invariant — while the file's own
    header asserts the whole thing is proven to fire. Splitting the groups into "checked against a
    service" and "declared third-party, with the reason" means a NEW group cannot land in neither.
    """
    rules = yaml.safe_load((REPO / "chart/alerting/rules.yml").read_text())
    groups = {g["name"] for g in rules["groups"]}

    assert groups, "no alert groups parsed — the check would pass vacuously"
    unclassified = sorted(groups - set(_ALERT_GROUP_SOURCES) - set(_THIRD_PARTY_ALERT_GROUPS) - set(_COLLECTOR_ALERT_GROUPS))
    assert not unclassified, (
        f"these alert groups are neither checked against a service's instruments, nor bound to a "
        f"Collector receiver, nor declared third-party: {unclassified}. Add the group's metrics module "
        "to _ALERT_GROUP_SOURCES, name the receiver that declares its series in _COLLECTOR_ALERT_GROUPS, "
        "or record in _THIRD_PARTY_ALERT_GROUPS whose exporter emits them."
    )
    stale = sorted((set(_ALERT_GROUP_SOURCES) | set(_THIRD_PARTY_ALERT_GROUPS) | set(_COLLECTOR_ALERT_GROUPS)) - groups)
    assert not stale, f"these groups are classified but no longer exist in rules.yml: {stale}"


@pytest.mark.parametrize("group_name", sorted(_ALERT_GROUP_SOURCES))
def test_every_first_party_ALERT_names_a_metric_the_service_actually_EMITS(group_name: str) -> None:
    """AN ALERT ON A SERIES NOBODY WRITES IS INDISTINGUISHABLE FROM AN ESTATE THAT IS HEALTHY.

    Widened 2026-08-22 from `lance-maintenance` alone to every first-party group. The narrow version
    was right about the mechanism and wrong about the blast radius: the same mistake in the lineage,
    medallion or notifications rules was unguarded, and self-concealing in exactly the same way.

    vmalert evaluates these against GreptimeDB. A rule whose PromQL names a metric no instrument
    creates never fires — and never fires is what a working alert looks like, so the mistake survives
    review. Checks the alert's metric names against the OTel instrument names, applying the
    OTLP->Prometheus convention the other direction: dots become underscores, a counter gains `_total`.
    """
    rules = yaml.safe_load((REPO / "chart/alerting/rules.yml").read_text())
    group = next((g for g in rules["groups"] if g["name"] == group_name), None)
    assert group is not None, f"no {group_name} alert group — its failures can never page"

    sources = [(REPO / path).read_text() for path in _ALERT_GROUP_SOURCES[group_name]]
    # The name AND its declared unit, because the unit changes the series name. `observable_gauge` is in
    # the alternation because the outbox age is one — an omission that would have made this whole check
    # vacuous for the very metric that motivated it. `gauge` (the SYNCHRONOUS one) joined it 2026-08-27
    # for the identical reason: ray-kit's `ray.control.jobs_known` is a plain `create_gauge`, so the
    # first run of this gate over the `ray` group reported a real instrument as a phantom. Third time
    # this file has been bitten by a matcher narrower than the values it must classify.
    instruments = [
        m
        for source in sources
        for m in re.findall(r'create_(?:counter|up_down_counter|histogram|observable_gauge|gauge)\(\s*"([^"]+)"(.*?)\)', source, re.DOTALL)
    ]
    assert instruments, f"parsed no instruments out of {_ALERT_GROUP_SOURCES[group_name]} — the check would pass vacuously"

    emitted: set[str] = set()
    for name, tail in instruments:
        base = name.replace(".", "_")
        unit = re.search(r'unit\s*=\s*"([^"]*)"', tail)
        suffix = _UCUM_SUFFIX.get(unit.group(1)) if unit else None
        # ONLY the suffixed spelling when the unit maps. Accepting both would defeat the point: the bare
        # name is not a series the backend ever produces, and tolerating it is what let the phantom pass.
        base = f"{base}_{suffix}" if suffix else base
        emitted.add(base)
        emitted.add(f"{base}_total")

    prefixes = sorted({name.split("_")[0] for name in emitted})
    # `[A-Za-z0-9_]+`, NOT `[a-z_]+`. A lowercase-only class silently TRUNCATES at the first capital,
    # so `medallion_stage_deniedTYPO_total` matched as `medallion_stage_denied` — a real emitted
    # metric — and the typo passed. Verified: the mutation that motivated this gate did not fail it
    # until the class was widened. This audit has now caught the identical bug twice in two different
    # gates, which is what makes it worth a comment rather than a quiet fix: a value pattern narrower
    # than the values it must reject will match a PREFIX of a bad value and call it good.
    pattern = r"\b((?:" + "|".join(prefixes) + r")_[A-Za-z0-9_]+)\b"
    referenced: set[str] = set()
    for rule in group["rules"]:
        referenced |= set(re.findall(pattern, rule["expr"]))
    assert referenced, f"the {group_name} rules reference no metric with any of its own prefixes {prefixes}"

    # Series emitted outside this repo, subtracted by NAMESPACE rather than by exempting the whole
    # group from the gate — which is the modelling error this replaces.
    phantom = sorted(name for name in referenced - emitted if not _is_external_series(name))
    assert phantom == [], (
        f"these {group_name} alerts query series no instrument emits, so they can never fire: {phantom}. "
        f"{list(_ALERT_GROUP_SOURCES[group_name])} creates {sorted(emitted)}"
    )


#: OTel env vars that take effect with NO `opentelemetry-instrument` launcher — verified by reading the
#: installed SDK, not by convention. The fleet runs `command: ["uvicorn"]`, so anything the LAUNCHER
#: consumes is inert there and copying it across would be cargo-cult symmetry.
#:
#:   OTEL_METRIC_EXPORT_INTERVAL        read by PeriodicExportingMetricReader.__init__
#:   OTEL_PYTHON_FASTAPI_EXCLUDED_URLS  read by the FastAPI instrumentor itself
#:
#: Deliberately EXCLUDED, and each was checked rather than assumed: OTEL_{TRACES,METRICS,LOGS}_EXPORTER
#: live in `opentelemetry.sdk._configuration`, which only the launcher runs; and
#: OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED is the launcher's switch for a call the fleet already
#: makes explicitly in `setup_otel`.
_LAUNCHER_FREE_OTEL_ENV: Final = ("OTEL_METRIC_EXPORT_INTERVAL", "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS")


def test_the_SSR_ZONES_export_telemetry_too() -> None:
    """The seven zones are the FIRST hop of every real user request, and they exported nothing.

    A browser reaches a SvelteKit/Bun server before it ever reaches the gateway, so a slow page with a
    fast gateway had no span anywhere to explain it. Worse than a gap: the RED dashboard's unfiltered
    `sum by (service_name)` renders their absence as "these services do not exist" rather than "these
    services are unmonitored" — the estate looks smaller than it is, and correctly so, because nothing
    was ever asked to report.

    Verified feasible before being asserted: the OTel Node SDK runs under Bun (spans created, correctly
    parented, and AsyncLocalStorage survives an await — which is what a per-request server span needs).
    Only MANUAL instrumentation is used; the auto-instrumentation packages rely on Node loader hooks Bun
    implements incompletely, and this estate does not need them.
    """
    docs = _rendered_docs("observability.enabled=true")
    zones = [doc for doc in docs if doc.get("kind") == "Deployment" and doc["metadata"]["name"].startswith("rask-web-")]
    assert len(zones) >= 7, f"expected the seven SSR zones, found {[d['metadata']['name'] for d in zones]}"

    missing = []
    for doc in zones:
        env = {
            e["name"]
            for container in doc["spec"]["template"]["spec"]["containers"]
            if not container["name"].startswith("daprd")
            for e in (container.get("env") or [])
        }
        if "OTEL_EXPORTER_OTLP_ENDPOINT" not in env or "OTEL_SERVICE_NAME" not in env:
            missing.append(doc["metadata"]["name"])

    assert not missing, (
        f"these SSR zones export no telemetry: {sorted(missing)}. They are the first hop of every browser "
        "request, so a slow page has no span to explain it — and the RED dashboard reads their silence as "
        "absence rather than as being unmonitored."
    )


def test_both_planes_agree_on_the_otel_env_that_ACTUALLY_APPLIES() -> None:
    """Two planes, two different answers to questions that have one right answer.

    The lakehouse pods carried a metric export interval and a probe-exclusion list; the fleet carried
    neither, so the estate exported metrics at two different cadences and the fleet's RED dashboard
    counted `/livez` and `/readyz` as traffic. Kubernetes probes every pod every few seconds, so on a
    quiet estate the health checks ARE the request rate — the panel reads busy and the latency
    distribution is dominated by a route nobody calls.

    Scoped to the vars that work WITHOUT the launcher. The fleet runs bare uvicorn, so asserting full
    symmetry would force in four vars that do nothing there — the same cargo-culting this audit has
    already cut twice.
    """
    docs = _rendered_docs("observability.enabled=true")

    def otel_env(workload: str) -> dict[str, str]:
        for doc in docs:
            if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == workload:
                for container in doc["spec"]["template"]["spec"]["containers"]:
                    if container["name"].startswith("daprd"):
                        continue
                    return {e["name"]: str(e.get("value", "")) for e in (container.get("env") or [])}
        raise AssertionError(f"{workload} did not render — this gate would pass vacuously")

    fleet, lance = otel_env("rask-gateway"), otel_env("release-name-catalog")

    for name in _LAUNCHER_FREE_OTEL_ENV:
        assert name in lance, f"the lakehouse plane lost {name} — this gate compares the two, so it cannot anchor on a missing value"
        assert fleet.get(name) == lance[name], (
            f"the two planes disagree on {name}: fleet={fleet.get(name)!r} lakehouse={lance[name]!r}. "
            "It takes effect without the launcher, so the fleet is not exempt — it is just unset."
        )


def test_every_PANEL_query_names_a_series_some_instrument_emits() -> None:
    """The other half of the phantom-series defect: alerts were gated, 34 panel queries were not.

    A dashboard panel naming a metric no instrument creates renders an empty chart, and an empty chart
    is what a quiet system looks like — so the mistake survives review exactly as it did for
    `outbox_oldest_age`, which read `> 300` on a series that has never existed.

    FIRST-PARTY NAMES ONLY. `dapr_*`, `ray_*` and the SDK's `http_*` families come from the sidecar,
    the Ray dashboard and the auto-instrumentation; this repo creates none of them and cannot check
    them from source. Their absence is a deployment fact, not a spelling one.

    Applies the same OTLP->Prometheus rules the alert gate learned: dots become underscores, a counter
    gains `_total`, and a declared UCUM unit is APPENDED — so an instrument with `unit="s"` is only
    ever queryable as `..._seconds`. Verified against the live store while this was written: the
    object-store histogram really is `..._bucket_total`, and a panel on the conventional `_bucket`
    would have found nothing.
    """
    sources = [path for paths in _ALERT_GROUP_SOURCES.values() for path in paths]
    sources += [
        "services/flows/src/flows/metrics.py",
        "services/ingest/src/ingest/metrics.py",
        "packages/service-kit/src/service_kit/bus_metrics.py",
    ]
    emitted: set[str] = set()
    for path in sources:
        text = (REPO / path).read_text()
        for name, tail in re.findall(r'create_(?:counter|up_down_counter|histogram|observable_gauge|gauge)\(\s*"([^"]+)"(.*?)\)', text, re.DOTALL):
            base = name.replace(".", "_")
            unit = re.search(r'unit\s*=\s*"([^"]*)"', tail)
            suffix = _UCUM_SUFFIX.get(unit.group(1)) if unit else None
            base = f"{base}_{suffix}" if suffix else base
            # Histograms reach PromQL through their derived series, and this estate's exporter appends
            # `_total` to those too — measured: lance_object_store_request_duration_seconds_bucket_total.
            emitted |= {base, f"{base}_total"} | {f"{base}_{part}{tot}" for part in ("bucket", "count", "sum") for tot in ("", "_total")}
    assert emitted, "parsed no instruments — this gate would pass vacuously"

    prefixes = sorted({name.split("_")[0] for name in emitted})
    pattern = re.compile(r"\b((?:" + "|".join(prefixes) + r")_[A-Za-z0-9_]+)\b")

    rendered = _helm_template("observability.enabled=true")
    phantom: set[str] = set()
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "ConfigMap":
            continue
        for key, value in (doc.get("data") or {}).items():
            if not key.endswith(".json") or not isinstance(value, str):
                continue
            phantom |= {name for name in set(pattern.findall(value)) - emitted if not _is_external_series(name)}

    assert not phantom, (
        f"these dashboard panels query first-party series no instrument emits, so they render empty: {sorted(phantom)}. "
        f"The instruments create {sorted(emitted)[:12]}... — remember a declared unit is APPENDED to the name."
    )


def test_every_service_app_entrypoint_CONFIGURES_LOGGING() -> None:
    """A module that builds a FastAPI app must configure application logging, or its logs vanish.

    Measured live 2026-08-17: lineage, catalog and both medallion apps each built `FastAPI(...)`
    directly and none of them called the logging setup, so every `getLogger(__name__)` record in the
    estate's three largest services propagated to a root logger with no handlers and was DISCARDED.
    Only uvicorn's own logs (which carry their own handlers) reached stdout.

    What that cost is the reason this gate exists rather than a convention: a malformed event
    POSTed to the running lineage ingest returned `{"status":"DROP"}` — the line right after
    `log.error(...)` — and produced no log line at all, verified three times. Every swallowed
    diagnostic in the estate had the same fate, including the WARNING that is the only signal a
    best-effort feed write ever failed.

    `make_service_app` callers are exempt: the factory calls it for them. This checks the modules
    that bypass the factory, which is exactly the set that regressed.
    """
    offenders: list[str] = []
    for path in sorted(REPO.glob("services/*/src/*/*.py")):
        text = path.read_text(encoding="utf-8")
        builds_app = "app = FastAPI(" in text
        if not builds_app:
            continue
        if "setup_logging()" in text or "make_service_app" in text:
            continue
        offenders.append(str(path.relative_to(REPO)))

    assert not offenders, (
        f"these modules build a FastAPI app without configuring logging: {offenders}\n\n"
        "Call `setup_logging()` from service_kit before building the app, or build it through "
        "`make_service_app`, which does it for you. Without it every application log record in that "
        "service is discarded and failures become invisible — the defect that hid a two-day lineage "
        "feed outage."
    )


def test_NO_stray_node_modules_at_the_repo_ROOT() -> None:
    """The JS plane lives in `frontend/`; a `node_modules` at the repo root shadows it.

    TypeScript resolves a bare specifier by walking UP the directory tree, so it does not stop at the
    workspace boundary. An install at the repo root is therefore visible to every zone's type-check,
    and whichever copy it finds first wins.

    Measured 2026-08-17, and it had been mistaken for real repo state long enough to be written down
    as a "known-red baseline": an orphaned 721 MB root `node_modules` — eslint, prettier,
    prettier-plugin-svelte and svelte 5.56.3, i.e. the toolchain deleted in the oxlint/oxfmt
    migration — made `svelte` resolve two ways at once. Every `Snippet` crossing the `@rask/ui`
    boundary then failed with "Two different types with this name exist, but they are unrelated", for
    45 errors in lakehouse, 6 in annotator, 6 in home and 2 in models. Moving that directory aside
    took the estate from 4 failing zones to 22/22 tasks green, changing no tracked file.

    It is invisible to CI (which installs fresh in a container) and invisible to git (`node_modules/`
    is ignored), so nothing else can report it. There is no root `package.json`, which is the tell:
    nothing declares these packages, so nothing can legitimately install them there.
    """
    stray = REPO / "node_modules"
    if not stray.exists():
        return

    entries = sorted(p.name for p in stray.iterdir() if not p.name.startswith("."))
    raise AssertionError(
        f"a stray node_modules exists at the repo root ({len(entries)} entries: {entries[:8]}…).\n\n"
        "The JS/TS plane is `frontend/` and there is no root package.json, so nothing declares these. "
        "TypeScript walks UP past the workspace when resolving, so this shadows frontend's own "
        "dependencies — a duplicate `svelte` here makes every Snippet crossing the @rask/ui boundary "
        "fail as two unrelated types.\n\n"
        "Remove it (`rm -rf node_modules` at the repo root). It is gitignored, nothing declares it, "
        "and `tests/e2e` + `frontend/` each carry their own."
    )


def test_the_shared_ray_image_carries_NO_WORKLOAD_dependencies() -> None:
    """The cluster base is the PLATFORM's image; a workload's stack belongs to the workload.

    `.docker/ray-cluster.dockerfile` used to build from `runners/htr/uv.lock`, so every lane on the
    shared cluster inherited torch, htrflow and ultralytics — and the platform's own `lance` had to
    be bolted on afterwards with hand-matched pins, because that workload lock contains no lance at
    all. Measured: not one of the three baked job scripts imports htrflow.

    CLAUDE.md's sealed-runner rule states it directly — "a workload's awkward dependencies are ITS
    problem, isolated by Ray Data / Ray Serve runtime environments, never by fattening a shared
    image" — and the same file records the owner ruling that this is what keeps the medallion lane
    shipped off.

    `runners/dummy` is the ONE exception and stays one: it is the estate's own GPU-free probe, it is
    COPYed as source rather than resolved as a second lock, and it adds no resolvable dependency
    (pyarrow and lance are already present from the platform package).
    """
    # Comment lines stripped first: the file explains IN PROSE why `runners/dummy` is copied rather
    # than synced, and a scanner that reads its own rationale as a violation is worse than no gate.
    lines = [ln for ln in (REPO / ".docker/ray-cluster.dockerfile").read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]

    resolved = re.findall(r"uv sync[^\n]*--project\s+(runners/\w+)", "\n".join(lines))
    assert not resolved, (
        f"the shared ray image resolves a workload lock: {resolved}\n\n"
        "Put the workload's dependencies in its own image (see .docker/ray-htr.dockerfile) or in a "
        "per-deployment Ray `runtime_env`, and keep this base agnostic. A second workload must cost "
        "a sibling dockerfile, never a fatter shared base."
    )


def test_the_ray_head_image_the_CHART_CONFIGURES_ships_the_tracing_hook_module() -> None:
    """The chart names two import paths; the image it configures must be able to import them.

      * `rayStartParams.tracing-startup-hook` is Ray CORE, imported inside worker/driver startup, so an
        ImportError surfaces as a head that will not come up — not as tracing quietly staying off.
      * `RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH` is Ray SERVE, which catches it and logs that the
        proxy/replica continues.

    Both name `service_kit.ray_tracing`, and that module's own docstring states the assumption it rests
    on: "installs ratch — so `service_kit` is importable in every Ray Python process on the cluster."
    That is a claim about ONE image, and nothing checked it. This does: the image named by
    `ray.image.repository` must really provide the module, whether by installing a distribution that
    depends on it or by carrying the source outright.

    WHY THIS IS WORTH A TEST EVEN THOUGH IT PASSES TODAY. The chart and the dockerfile only contradict
    each other at pod start — the chart renders fine, the image builds fine, and the mismatch is
    invisible to every other gate. It is also a live hazard rather than a hypothetical one: the head
    deployed on 2026-08-23 was built before `ray-cluster.dockerfile` switched to a root-lock
    `uv sync --package` build at all, and importing the then-platform package on it returned
    ModuleNotFoundError. This test cannot see a stale DEPLOYED image, but it does pin the contract
    the next build must satisfy.

    Deliberately NOT asserted of `ray-lance`. It bakes the same job scripts, so it looks like a head
    image, but the chart never points at it and its jobs deliberately MIRROR small pieces of service_kit
    rather than import them — it is built on a py312 base where service-kit (>=3.13) cannot install at
    all. Demanding the hook of it would be inventing a requirement.
    """
    values = yaml.safe_load((REPO / "chart" / "values.yaml").read_text())
    configured = values["ray"]["image"]["repository"]

    dockerfile = REPO / ".docker" / f"{configured}.dockerfile"
    assert dockerfile.exists(), f"chart ray.image.repository is {configured!r} but .docker/{configured}.dockerfile does not exist"

    rayservice = (REPO / "chart" / "templates" / "rayservice.yaml").read_text()
    modules = {m.group(1) for m in re.finditer(r'"([\w.]+):(?:setup_tracing|serve_span_processors)"', rayservice)}
    assert modules, "the chart names no Ray tracing hook at all — if the hooks were removed, remove this test with them"

    text = dockerfile.read_text()
    for module in sorted(modules):
        copied = f"packages/service-kit/src/{module.replace('.', '/')}.py" in text
        # The deps-only member that NAMES the image's environment (open_ray-kernel.md move 13; it
        # replaced `--package ratch` at the dissolution). The member's pyproject must actually
        # depend on service-kit, or the sync provides the name and not the module.
        via_member = "uv sync --package ray-cluster-env" in text and "service-kit" in (REPO / "packages" / "ray-cluster-env" / "pyproject.toml").read_text()
        installed = re.search(r"(pip install|uv (pip )?install)[^\n]*service-kit", text) is not None

        assert copied or via_member or installed, (
            f"{dockerfile.name} backs the Ray head the chart configures, but nothing in it makes {module!r} "
            f"importable. Ray CORE imports the startup hook during boot, so this is a head that fails to "
            f"start, not tracing that stays off."
        )


@pytest.mark.parametrize("posture", TELEMETRY_POSTURES)
def test_every_pod_that_exports_otlp_logs_is_labelled_as_such(posture: tuple[str, ...]) -> None:
    """The label and the exporter are two halves of one decision, and nothing held them together.

    `lance.dev/logs: otlp` tells the Collector "this pod's stdout is a DUPLICATE — drop the file-tailed
    copy". That is true exactly when the pod's SDK really exports logs over OTLP, and the two drifted the
    moment 5dae4538 gave `service_kit.setup_otel` a real `LoggerProvider`: six fleet pods became genuine
    OTLP log producers while carrying no label, so every record they emit is stored TWICE. Measured live
    the same day — one httpx request, both copies in `opentelemetry_logs`:

        scope_name='httpx' body='HTTP Request: POST http://localhost:3500/v1.0/state/... "204 No Content"'
        scope_name=''      body='2026-08-23 06:45:13,634 INFO [httpx] ... - HTTP Request: POST ...'

    Asserted as an EQUALITY because both directions are defects, and the second is far worse than the
    first: an exporter without the label double-ingests, but a label without an exporter deletes a whole
    pod's logs and the pipeline still reports healthy.

    KEYED ON LOG EXPORT, NOT ON ANY OTLP ENDPOINT — a distinction this gate learned the day the SSR zones
    started exporting. They emit traces and no logs, so an endpoint-only test read them as log producers
    and would have had them labelled, deleting the stdout of seven pods that have no other copy.

    Mechanical and drift-proof: the opt-in vars are rendered by exactly `rask.otelEnv` and
    `lance.otelEnv`, so a new workload is covered the day it is added rather than the day someone
    remembers this test.

    PARAMETRISED OVER THE POSTURE, because pinning only the healthy one is how this test missed a live
    defect it was written to catch. It originally rendered `observability.enabled=true` alone; under
    the chart's documented EXTERNALISE posture the six fleet pods carried the "my logs are already
    exported, drop the file-tailed copy" label while `rask.otelEnv` rendered no exporter at all — the
    exact `labelled=True, exports_otlp=False` direction this test's own message calls "the pod goes
    dark while every pipeline reports healthy".
    """
    offenders = []
    for doc in _rendered_docs(*posture):
        if doc.get("kind") not in {"Deployment", "StatefulSet", "DaemonSet"}:
            continue
        template = doc["spec"]["template"]
        # LOGS specifically, not "any OTLP". The label means "my log records already reach the store by
        # another route, so drop the file-tailed copy" — so the question is whether this pod exports
        # LOGS, and an endpoint alone does not answer it. The SSR zones export TRACES ONLY (a
        # NodeTracerProvider, no logger provider), so labelling them on the strength of an endpoint would
        # delete the stdout of seven pods that have no other copy — the precise defect slice 5 measured
        # at 0 survivors of 10.
        #
        # Two spellings because the planes opt in differently: the lakehouse pods run under the launcher
        # and declare OTEL_LOGS_EXPORTER, while the fleet builds a real LoggerProvider inside
        # `setup_otel`, gated on RASK_OTEL_ENABLED.
        names = {env.get("name") for container in template["spec"]["containers"] for env in container.get("env") or []}
        exports = bool(names & {"OTEL_LOGS_EXPORTER", "RASK_OTEL_ENABLED"})
        labelled = ((template.get("metadata") or {}).get("labels") or {}).get("lance.dev/logs") == "otlp"
        if exports != labelled:
            offenders.append(f"{doc['metadata']['name']}: exports_otlp={exports} labelled={labelled}")

    assert not offenders, (
        "these workloads disagree with themselves about whether their logs are already exported:\n  "
        + "\n  ".join(offenders)
        + "\n\nexports_otlp=True labelled=False -> every log record is stored twice.\n"
        "exports_otlp=False labelled=True -> the Collector deletes that pod's file-tailed logs and it has "
        "no OTLP copy, so the pod goes dark while every pipeline reports healthy."
    )


def test_the_daprd_sidecar_logs_are_json_and_parsed() -> None:
    """Two halves that are only correct together, and either alone is dead config.

    The Collector's filelog `json_parser` is scoped to the daprd container so that
    `WHERE severity_text = 'ERROR'` can find a sidecar failure at all, and so `scope`/`app_id` become
    queryable columns. It fires only if daprd actually emits JSON, which is `dapr.io/log-as-json`. Ship
    the annotation without the parser and the sidecar plane stays unparsed logfmt; ship the parser without
    the annotation and it never matches a single record.

    The parser is deliberately NOT estate-wide. Every other stream on the node has its own shape — NATS,
    OpenFGA, CloudNativePG, the Dapr control plane, Dex, uvicorn — and an unscoped parser or recombine
    would corrupt them. daprd is the one container here with both a fixed schema and no OTLP twin.
    """
    docs = _rendered_docs("observability.enabled=true")

    annotated = [
        doc["metadata"]["name"]
        for doc in docs
        if doc.get("kind") in {"Deployment", "StatefulSet"}
        and ((doc["spec"]["template"].get("metadata") or {}).get("annotations") or {}).get("dapr.io/enabled") == "true"
    ]
    assert annotated, "no dapr-annotated workloads rendered — this gate would pass vacuously"

    for doc in docs:
        if doc.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        annotations = (doc["spec"]["template"].get("metadata") or {}).get("annotations") or {}
        if annotations.get("dapr.io/enabled") != "true":
            continue
        assert annotations.get("dapr.io/log-as-json") == "true", (
            f"{doc['metadata']['name']} injects a Dapr sidecar but does not ask it for JSON logs. daprd then "
            "ships logrus text, the Collector's json_parser never matches, and a component-init failure is "
            "the same shape as a routine startup line — no severity, no scope, no app_id."
        )

    operators = _collector_config(docs)["receivers"]["filelog"]["operators"]
    parsers = [op for op in operators if op.get("type") == "json_parser"]
    assert parsers, (
        "the filelog receiver parses nothing. `dapr.io/log-as-json` then produces JSON that is stored as an "
        "opaque body string: severity_text stays empty and no query can find a sidecar ERROR."
    )
    assert any('== "daprd"' in (op.get("if") or "") for op in parsers), (
        "a json_parser is present but not scoped to the daprd container. Every other stream on this node has "
        "its own shape, and an unscoped parser corrupts them — scope it with `if`."
    )


# --------------------------------------------------------------------------------------------------
# Alert rules must be evaluable by the engine that ACTUALLY evaluates them
# --------------------------------------------------------------------------------------------------
#: `make alert-rules-check` runs promtool -- PROMETHEUS's engine. Production runs vmalert against
#: GreptimeDB's PromQL endpoint, and the two do not accept the same language. Everything below is a
#: shape gate for the differences that have already cost the estate a rule, measured against the live
#: store (GreptimeDB v1.1.1) at 10.43.90.225:4000/v1/prometheus/api/v1/query.


#: Every first-party metrics module in the estate, and the alert group that must read it.
#:
#: THE REVERSE DIRECTION, and the estate had only the forward one. `test_every_first_party_ALERT_names
#: _a_metric_the_service_actually_EMITS` asks "does this rule name a real instrument?" — which catches
#: a typo and cannot catch an instrument NOBODY ALERTS ON. Measured 2026-08-26: `grep 'ingest_\|flows_'
#: chart/alerting/rules.yml` returned zero, and both modules exist precisely because Dapr's own
#: workflow families report `status="success"` for a run that returned FAILED. The fact they were
#: added to carry reached nobody.
#:
#: An entry is a CLAIM that the named group reads that module's instruments. Adding a metrics module
#: without a group fails here, which is the point: an instrument with no rule is telemetry, not
#: monitoring.
_METRICS_MODULE_GROUPS: dict[str, str] = {
    "services/lineage/src/lineage/core/metrics.py": "lance-lineage",
    "services/medallion/src/medallion/core/metrics.py": "lance-medallion",
    "services/notifications/src/notifications/api/metrics.py": "lance-notifications",
    "services/maintenance/src/maintenance/core/metrics.py": "lance-maintenance",
    "services/ingest/src/ingest/metrics.py": "lance-ingest",
    "services/flows/src/flows/metrics.py": "lance-flows",
    "packages/service-kit/src/service_kit/lakehouse/outbox_metrics.py": "lance-lineage",
}


def test_every_FIRST_PARTY_INSTRUMENT_is_read_by_some_alert_rule() -> None:
    """An instrument nothing alerts on is telemetry, not monitoring.

    The forward gate (a rule must name a real instrument) cannot catch this, and the consequence was
    concrete: `ingest/metrics.py` states the gap in its OWN DOCSTRING — "chart/alerting/rules.yml duly
    contains zero ingest rules: no page fires for a run that failed, a fan-out that stalled, or units
    that never landed" — and that sentence sat there true for as long as the file existed.

    Checks the MODULE's declared instruments against the named group's expressions, applying the
    OTLP->Prometheus convention: dots become underscores and a counter gains `_total`. A module whose
    group reads none of its instruments fails, naming them.
    """
    rules = yaml.safe_load((REPO / "chart/alerting/rules.yml").read_text())
    by_name = {g["name"]: g for g in rules["groups"]}

    unread: list[str] = []
    for module, group_name in sorted(_METRICS_MODULE_GROUPS.items()):
        source = (REPO / module).read_text()
        instruments = re.findall(r'create_(?:counter|up_down_counter|histogram|observable_gauge|gauge)\(\s*"([^"]+)"', source)
        assert instruments, f"parsed no instruments out of {module} — this check would pass vacuously"

        group = by_name.get(group_name)
        assert group is not None, f"{module} names alert group {group_name!r}, which rules.yml does not define"
        exprs = " ".join(str(r.get("expr", "")) for r in group.get("rules", []))

        # `ingest.runs` -> `ingest_runs`; a counter also answers to `_total`.
        read = [i for i in instruments if i.replace(".", "_") in exprs]
        if not read:
            flat = sorted({i.replace(".", "_") for i in instruments})
            unread.append(f"{module} -> group {group_name!r} reads none of {flat}")

    assert not unread, (
        f"{len(unread)} first-party metrics module(s) are emitted and alerted on by nothing, so the "
        f"failures they exist to carry reach no one:\n" + "\n".join(f"  - {u}" for u in unread)
    )


def _alert_exprs() -> list[tuple[str, str]]:
    """(alert name, expr) for every rule in the shipped file."""
    rules = yaml.safe_load((REPO / "chart/alerting/rules.yml").read_text())
    return [(rule["alert"], str(rule["expr"])) for group in rules["groups"] for rule in group.get("rules", []) if "alert" in rule]


def _without_string_literals(expr: str) -> str:
    """Drop quoted label values, so a matcher like `{lane=~"feed|or"}` cannot look like an operator."""
    return re.sub(r"'[^']*'|\"[^\"]*\"", '""', expr)


def test_no_alert_rule_combines_absent_with_a_set_operator() -> None:
    """`X == 0 or absent(X)` is valid PromQL and GreptimeDB REFUSES it, so the rule can never fire.

    Measured against the live store, both forms of the estate's own two composites:

        sum(increase(compaction_runs_total[30m])) == 0 or absent(compaction_runs_total)   -> HTTP 500
        sum(rate(notifications_ingress_events_total{...}[30m])) == 0 or absent(...)       -> HTTP 400

    while each half on its own returns 200. promtool reports `SUCCESS: 23 rules found` over exactly
    these, because Prometheus evaluates them fine -- which is the whole problem: the gate proves the
    rules against an engine that is not the one in production.

    This is a worse failure than a rule that is merely wrong. Both of these were written DELIBERATELY,
    by someone who had understood the bug: the comments above them at rules.yml:198-201 and :240-242
    spell out "ABSENT IS NOT ZERO", cite the DaprSchedulerMetricsMissing precedent, and explain that a
    `== 0` over an emptied vector goes silent exactly when the emitting pod dies. The reasoning was
    right and the expression was unevaluable, so the fix for going-silent-when-it-matters was itself
    silent. The file's own working precedent is a bare `absent()` in its OWN rule (:366, :446).
    """
    offenders = []
    for name, expr in _alert_exprs():
        stripped = _without_string_literals(expr)
        if "absent(" in stripped and re.search(r"\b(or|and|unless)\b", stripped):
            offenders.append(f"{name}: {expr}")

    assert not offenders, (
        f"{len(offenders)} alert rule(s) use absent() as an operand of a set operator. GreptimeDB "
        f"rejects the whole expression, so the rule NEVER fires and its silence is indistinguishable "
        f"from health:\n" + "\n".join(f"  - {o}" for o in offenders) + "\n"
        "Split it into two rules: the `== 0` condition, and a standalone `absent(...)` rule "
        "(the shape already used at rules.yml:366 and :446)."
    )


def test_no_alert_rule_uses_absent_over_time() -> None:
    """`absent_over_time` is the tempting fix for the above, and on GreptimeDB it is SILENT.

    Measured: `absent_over_time(nonexistent_xyz_metric[10m])` returns `status: success` with an EMPTY
    result, where Prometheus returns 1. So it evaluates, it is green, and it can never fire -- strictly
    worse than the HTTP 500 it would replace, because nothing at all reports it. `absent(...)` on the
    same non-existent metric correctly returns 1 (verified in the same session).
    """
    offenders = [f"{name}: {expr}" for name, expr in _alert_exprs() if "absent_over_time(" in expr]
    assert not offenders, (
        "absent_over_time() returns an empty vector on GreptimeDB where Prometheus returns 1, so these "
        "rules are evaluable, green, and dead:\n" + "\n".join(f"  - {o}" for o in offenders) + "\n"
        "Use a bare absent() in its own rule instead."
    )


def _scrape_job_names() -> set[str]:
    """The `job_name:` values the OTel Collector's prometheus receiver actually declares."""
    text = (REPO / "chart/templates/otel-collector.yaml").read_text()
    return set(re.findall(r"^\s*-\s*job_name:\s*(\S+)\s*$", text, re.MULTILINE))


def test_every_job_selector_names_a_real_scrape_job() -> None:
    """A rule selecting `job="X"` where nothing scrapes X is a rule that can never fire.

    The estate already has the series-level version of this gate (the phantom-metric checks above).
    This is the TARGET-level version, and it is the one that matters for anything self-monitoring:
    a metric name can be real and universally emitted, and the rule still be dead because no scrape
    job attaches that `job` label. `up{job="greptimedb"}` was exactly that until the scrape job landed
    on 2026-08-23 — the metric `up` exists estate-wide, and the selector matched nothing.

    It binds both directions, which is the point: the GreptimeDB* rules cannot ship without the
    scrape job, and the scrape job cannot be deleted while the rules that depend on it are here.
    """
    declared = _scrape_job_names()
    assert declared, "parsed no job_name out of otel-collector.yaml — this check would pass vacuously"

    dangling = []
    for name, expr in _alert_exprs():
        for job in re.findall(r'job\s*=~?\s*"([^"]+)"', expr):
            if job not in declared:
                dangling.append(f'{name}: job="{job}"')

    assert not dangling, (
        f"{len(dangling)} alert rule selector(s) name a scrape job that otel-collector.yaml does not "
        f"declare, so they match nothing and can never fire:\n" + "\n".join(f"  - {d}" for d in dangling) + f"\ndeclared jobs: {sorted(declared)}"
    )


def test_the_greptimedb_memory_threshold_tracks_the_chart_limit() -> None:
    """GreptimeDBMemoryHigh's byte literal must stay a sane fraction of the pod's memory limit.

    rules.yml is mounted with `.Files.Get`, so it CANNOT be templated — the threshold is a hardcoded
    literal and the limit lives in values.yaml. Nothing otherwise relates them, so raising the limit
    to 16Gi would silently leave a warning that fires at 37% of capacity, and lowering it to 4Gi would
    leave one that can only fire after the OOM it exists to pre-empt.
    """
    exprs = dict(_alert_exprs())
    expr = exprs.get("GreptimeDBMemoryHigh")
    assert expr, "GreptimeDBMemoryHigh is gone — delete this gate with it, do not leave it passing vacuously"
    match = re.search(r">\s*(\d+)\s*$", expr)
    assert match, f"GreptimeDBMemoryHigh no longer ends in a byte literal: {expr!r}"
    threshold = int(match.group(1))

    values = yaml.safe_load((REPO / "chart/values.yaml").read_text())
    raw = values["greptimedb-standalone"]["resources"]["limits"]["memory"]
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
    suffix = next((u for u in units if str(raw).endswith(u)), None)
    assert suffix, f"unrecognised memory limit {raw!r} — teach this gate the unit rather than skipping it"
    limit = int(str(raw)[: -len(suffix)]) * units[suffix]

    ratio = threshold / limit
    assert 0.60 <= ratio <= 0.85, (
        f"GreptimeDBMemoryHigh fires at {threshold} bytes, which is {ratio:.0%} of the configured "
        f"limit {raw}. Below 60% it is noise; above 85% it cannot pre-empt the OOMKill it exists for. "
        f"Move the literal in chart/alerting/rules.yml when you move the limit in chart/values.yaml."
    )


def _collector_declared_metrics(receiver: str) -> set[str]:
    """The `metric_name` values a sqlquery receiver declares in the RENDERED collector config.

    Rendered, not grepped out of the template: the receiver is gated, so a text match would keep
    passing after a values change that stops it shipping — the exact shape of "the rule is fine and
    nothing produces its series".
    """
    config = _collector_config(_rendered_docs())
    return {
        metric["metric_name"]
        for query in (config.get("receivers", {}).get(receiver) or {}).get("queries", [])
        for metric in query.get("metrics", [])
        if "metric_name" in metric
    }


@pytest.mark.parametrize("group_name", sorted(_COLLECTOR_ALERT_GROUPS))
def test_every_collector_sourced_ALERT_names_a_metric_the_RECEIVER_declares(group_name: str) -> None:
    """The target-level gate for series the Collector itself produces.

    `test_every_job_selector_names_a_real_scrape_job` binds a rule to a SCRAPE job; nothing bound a
    rule to a receiver that synthesises metrics without scraping anything, and a sqlquery receiver is
    exactly that. Both directions matter and both are asserted: a rule may not name an undeclared
    metric, and a declared metric that no rule reads is a query running every collection interval for
    nobody.

    The match pattern is derived from the prefixes actually DECLARED, the same way the first-party
    gate derives its own — a fixed prefix list would silently stop matching the day the metrics are
    renamed, which is the failure this class of gate exists to prevent.
    """
    receiver = _COLLECTOR_ALERT_GROUPS[group_name]
    declared = _collector_declared_metrics(receiver)
    assert declared, f"the {receiver} receiver declares no metrics at all, so every {group_name} rule is dead"

    prefixes = {"_".join(name.split("_")[:2]) for name in declared}
    rules = yaml.safe_load((REPO / "chart/alerting/rules.yml").read_text())
    group = next((g for g in rules["groups"] if g["name"] == group_name), None)
    assert group is not None, f"no {group_name} alert group — its failures can never page"

    referenced: set[str] = set()
    dangling: list[str] = []
    for rule in group.get("rules", []):
        expr = str(rule.get("expr", ""))
        for token in re.findall(r"\b[a-z_][a-z0-9_]*\b", expr):
            if not any(token.startswith(prefix) for prefix in prefixes):
                continue
            referenced.add(token)
            if token not in declared:
                dangling.append(f"{rule['alert']}: {token}")

    assert not dangling, (
        f"{len(dangling)} {group_name} rule(s) name a metric the `{receiver}` receiver does not "
        f"declare, so they can never fire:\n" + "\n".join(f"  - {d}" for d in dangling) + f"\ndeclared: {sorted(declared)}"
    )
    unread = sorted(declared - referenced)
    assert not unread, (
        f"the `{receiver}` receiver declares {unread} and no {group_name} rule reads them — a query "
        "billed to every collection interval, answering nothing. Alert on it or stop collecting it."
    )


def test_the_workflow_history_query_matches_the_REAL_key_shape() -> None:
    """Dapr's own protocol reference documents a key format the Postgres state store does not use.

    The doc says history keys are `wf-history-<instance_id>-<index>`. Measured against the live store:
    `key like '%wf-history-%'` matched 0 rows and `key like '%||history-%'` matched 5551. The documented
    form is the LOGICAL key; the state store holds `<app>||<actor-type>||<id>||history-000006`.

    So the most likely way this metric dies is someone reading the official doc and "fixing" the filter
    to agree with it. The gauge would go to a permanent 0, DaprWorkflowHistoryNotCollected could never
    fire, and a dead alert is indistinguishable from a healthy estate — the `outbox_oldest_age` defect
    exactly. This gate makes that edit fail instead of shipping silently.
    """
    config = _collector_config(_rendered_docs())
    queries = (config.get("receivers", {}).get("sqlquery/daprstate") or {}).get("queries", [])
    assert queries, "the sqlquery/daprstate receiver declares no queries — nothing feeds the retention alert"

    sql = " ".join(str(query.get("sql", "")) for query in queries)
    assert "||history-" in sql, (
        "the workflow-history query no longer filters on the REAL key shape `%||history-%`. Dapr's "
        "protocol reference documents `wf-history-<id>-<index>`, which matches ZERO rows in the "
        "Postgres state store — a filter taken from the doc makes the gauge a permanent 0 and "
        "DaprWorkflowHistoryNotCollected unable to fire."
    )
    assert "wf-history-" not in sql, (
        "the workflow-history query uses the DOCUMENTED key prefix `wf-history-`, which matches nothing "
        "in the Postgres state store (measured: 0 rows vs 5551 for `%||history-%`). The alert built on "
        "it can never fire."
    )


def _duration_seconds(value: str) -> int:
    """`720h` / `90m` -> seconds. The only two units the chart's own render guard admits."""
    match = re.fullmatch(r"([1-9][0-9]*)(h|m)", str(value))
    assert match, f"unrecognised retention duration {value!r} — teach this gate the unit rather than skipping it"
    return int(match.group(1)) * (3600 if match.group(2) == "h" else 60)


def test_the_workflow_history_alert_threshold_TRACKS_the_retention_policy() -> None:
    """DaprWorkflowHistoryNotCollected's literal must stay just above the LONGEST retention window.

    Same coupling problem as GreptimeDBMemoryHigh, same reason it needs a test: rules.yml is mounted
    with `.Files.Get` and cannot be templated, so the threshold is a hardcoded number while the policy
    it is derived from lives in values.yaml. Lengthen `failed` to 2160h and the alert starts firing on
    a correctly-retained estate; shorten every window to 24h and it can only fire a month after
    collection has already stopped.

    The band is deliberately tight on the low side. The threshold must exceed the longest window —
    below it, rows the policy is still entitled to keep look like a failure — but not by so much that
    the alert waits weeks to notice the scheduler has stopped collecting.
    """
    exprs = dict(_alert_exprs())
    expr = exprs.get("DaprWorkflowHistoryNotCollected")
    assert expr, "DaprWorkflowHistoryNotCollected is gone — delete this gate with it, do not leave it passing vacuously"
    match = re.search(r">\s*(\d+)\s*$", expr)
    assert match, f"DaprWorkflowHistoryNotCollected no longer ends in a seconds literal: {expr!r}"
    threshold = int(match.group(1))

    values = yaml.safe_load((REPO / "chart/values.yaml").read_text())
    retention = values["dapr"]["workflowRetention"]
    longest = max(_duration_seconds(retention[state]) for state in ("completed", "failed", "terminated"))

    ratio = threshold / longest
    assert 1.05 <= ratio <= 1.50, (
        f"DaprWorkflowHistoryNotCollected fires at {threshold}s, which is {ratio:.2f}x the longest "
        f"configured retention window ({longest}s). At or below 1.0x it fires on history the policy is "
        f"still entitled to keep; above 1.5x it waits weeks to report that collection has stopped. "
        f"Move the literal in chart/alerting/rules.yml when you move dapr.workflowRetention in "
        f"chart/values.yaml."
    )


def _slugify_heading(text: str) -> str:
    """A markdown heading -> its anchor, the way GitHub and the docs site both derive it.

    Characters that are neither alphanumeric nor space nor hyphen are DROPPED rather than replaced,
    which is why `DLQ parking -> a delivery gave up` anchors as `dlq-parking--a-delivery-gave-up`
    with a double hyphen. Getting that wrong would make this gate reject links that work.
    """
    kept = "".join(char for char in text.lower() if char.isalnum() or char in " -")
    return kept.strip().replace(" ", "-")


def test_every_RUNBOOK_LINK_in_an_alert_annotation_resolves() -> None:
    """A dead runbook link in an alert is worse than no link: it costs the on-caller time at 3am.

    This is not hypothetical. `e1b8f3dd` moved the runbooks into docs/runbooks/ and left every
    annotation in rules.yml pointing at the old flat path -- 24 of 29 rules, for weeks, and nothing
    reported it. Every other gate in this file passed the whole time, because a link is prose and
    prose is not evaluated.

    Both halves are checked, because both fail the same way. The FILE must exist, and the ANCHOR must
    be a heading in it -- a heading rename leaves a link that loads the right page and lands nowhere,
    which reads as "the runbook does not cover this".
    """
    rules = yaml.safe_load((REPO / "chart/alerting/rules.yml").read_text())
    annotations = [str(value) for group in rules["groups"] for rule in group.get("rules", []) for value in (rule.get("annotations") or {}).values()]
    assert annotations, "parsed no annotations out of rules.yml -- this check would pass vacuously"

    links = {link for text in annotations for link in re.findall(r"docs/[A-Za-z0-9_./-]+\.md(?:#[A-Za-z0-9-]+)?", text)}
    assert links, "no runbook links found in any annotation -- this check would pass vacuously"

    broken: list[str] = []
    for link in sorted(links):
        path, _, fragment = link.partition("#")
        target = REPO / path
        if not target.exists():
            broken.append(f"{link} -> no such file")
            continue
        if not fragment:
            continue
        headings = {_slugify_heading(m) for m in re.findall(r"^#{1,6}\s+(.*?)\s*$", target.read_text(), re.MULTILINE)}
        if fragment not in headings:
            broken.append(f"{link} -> no heading anchors to #{fragment}")

    assert not broken, f"{len(broken)} runbook link(s) in alert annotations do not resolve, so the alert points an on-caller at nothing:\n" + "\n".join(
        f"  - {b}" for b in broken
    )


def test_every_alert_rule_has_a_promtool_case() -> None:
    """rules.yml and rules_test.yml are related by nothing, so both directions rot silently.

    `promtool check rules` reports `SUCCESS: N rules found` whether or not any of them is exercised,
    and `promtool test rules` passes a file that tests three of twenty-nine. So deleting an alert
    together with its case leaves every gate green, and adding an alert with no case does too — the
    second is how six rules landed untested in this very change until this gate was added.

    Both directions are asserted. A case naming an alert that no longer exists is the same defect
    wearing the other hat: it reads as coverage and exercises nothing.
    """
    rules = yaml.safe_load((REPO / "chart/alerting/rules.yml").read_text())
    declared = {r["alert"] for g in rules["groups"] for r in g.get("rules", []) if "alert" in r}
    assert declared, "parsed no alerts out of rules.yml — this check would pass vacuously"

    tests = yaml.safe_load((REPO / "chart/alerting/rules_test.yml").read_text())
    exercised = {case["alertname"] for group in tests.get("tests", []) for case in group.get("alert_rule_test", []) if "alertname" in case}
    assert exercised, "parsed no alertname out of rules_test.yml — this check would pass vacuously"

    untested = sorted(declared - exercised)
    phantom = sorted(exercised - declared)
    assert not untested, f"{len(untested)} alert rule(s) have no promtool case, so nothing proves they fire (or stay quiet) under any series at all: {untested}"
    assert not phantom, f"{len(phantom)} promtool case(s) name an alert that rules.yml does not define — they read as coverage and exercise nothing: {phantom}"


def test_no_coroutine_verifies_a_bearer_on_the_event_loop() -> None:
    """A fourth auth door must not be able to reintroduce the stall that ING-02 fixed twice.

    `OIDCVerifier.verify` is synchronous and, on a cold cache or a key rotation, does OIDC discovery
    plus a JWKS fetch over the network. From a plain `def` route that is CORRECT — FastAPI threadpools
    it — so this guard fires only inside `async def`, where an inline call stalls the whole worker:
    every in-flight request in the pod, and any liveness probe mounted on the same app.

    The motivating history is the one this file exists for. The fix was written on the ingest door
    (`open_python-audit` ING-02) and verified there; the medallion door is a ~120-line COPY of the same
    function (DUP-03) and went on blocking the cascade head, plus a second copy in the promotion door.
    Two of the three call sites were wrong while the claim "the blocking-auth defect is fixed" was
    true of the one that had been looked at. Coroutines must go through
    `service_kit.governed.oidc.verify_off_loop`, which owns the hop.
    """
    roots = [SERVICES, REPO / "packages"]
    offenders: list[str] = []
    for py in sorted(path for root in roots for path in root.rglob("*.py")):
        if "/tests/" in py.as_posix():
            continue
        try:
            tree = ast.parse(py.read_text(errors="ignore"))
        except SyntaxError:  # a runner pinned to an older grammar is not this guard's business
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for inner in ast.walk(node):
                # A CALL to `<something verifier-ish>.verify(...)`. A bare reference is fine — that is
                # exactly how `verify_off_loop` hands the callable to `asyncio.to_thread`.
                if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
                    continue
                if inner.func.attr != "verify":
                    continue
                target = inner.func.value
                name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
                if "verif" in name.lower():
                    offenders.append(f"{py.relative_to(REPO)}:{inner.lineno} in async def {node.name}")
    assert not offenders, (
        "these coroutines call verifier.verify() inline, stalling the event loop; "
        "await service_kit.governed.oidc.verify_off_loop(verifier, raw) instead:\n  " + "\n  ".join(offenders)
    )


def test_dapr_hot_reload_is_off_because_this_estate_cannot_converge_it() -> None:
    """The sidecars' `lance-tracing` Configuration must disable HotReload, unconditionally.

    Dapr 1.18 promoted HotReload to GA and default-ON, and every sidecar in this estate references
    this one Configuration. Each then runs a 60-second reconcile ticker that diffs the operator's
    components against its own in-memory store — and for `lance-statestore` that diff can never come
    out equal, because its DSN arrives as a `secretKeyRef`: the operator holds the REFERENCE, daprd
    holds the RESOLVED value. So the reconciler concludes it changed on every tick, tries to
    hot-reload it, and refuses, because Dapr will not hot-reload a component used as an actor state
    store.

    MEASURED on the live estate before the fix: exactly 30 `Aborting to hot-reload a state store
    component that is used as an actor state store: lance-statestore` lines per sidecar per 30
    minutes — one a minute, across 16 sidecars, roughly 540 ERROR lines an hour. Nothing was pending
    and nothing was broken; the loop is structurally unable to converge, and it buried every real
    error in the estate's error stream. After the fix, and a restart: `Enabled features:
    WorkflowsRemoteActivityReminder`, and zero aborts.

    Pinned as a test because the failure is SILENT and reads as healthy: the flag defaults ON, so
    deleting this block restores a permanent error flood that no gate would otherwise notice, and
    whose only symptom is that genuine errors get harder to find. Asserted on the RENDERED chart
    rather than the template text so a conditional accidentally wrapping the block also fails.
    """
    rendered = _helm_template()
    configs = [
        doc for doc in yaml.safe_load_all(rendered) if doc and doc.get("kind") == "Configuration" and doc.get("metadata", {}).get("name") == "lance-tracing"
    ]
    assert configs, "the sidecars' `lance-tracing` Dapr Configuration is not rendered at all"

    features = {f["name"]: f.get("enabled") for f in (configs[0].get("spec", {}).get("features") or [])}
    assert features.get("HotReload") is False, (
        "HotReload is not disabled in `lance-tracing`, so every sidecar will retry an "
        f"unconvergeable reload of the actor state store once a minute, forever. features={features}"
    )


def test_every_privileged_identity_has_a_dedicated_credential_seeded() -> None:
    """A privileged subject with no `service-token-<identity>` is a fail-closed outage, not a downgrade.

    `service_kit.governed.dapr_auth` binds a PRIVILEGED service identity to its own dedicated
    credential; every other identity authenticates with the estate's SHARED `APP_API_TOKEN`. Until
    2026-08-26 nothing rendered `*_PRIVILEGED_SUBJECTS` — `grep -rn PRIVILEGED chart/` matched a single
    comment — so the control was inert in every deployment the chart produced, and any holder of the
    shared token could authenticate as any name on `LANCE_SERVICE_SUBJECTS`. Those names hold `owner`
    on every warehouse (`LANCE_FGA_CASCADE_WRITERS`), which carries `can_drop`, `can_deregister`,
    `can_restore` and `manage_grants` across every tenant.

    The two halves must be rendered from ONE derivation, and this pins that they are. Rendering the
    subject list without seeding a token turns each privileged service into a hard refusal
    (`privileged but has no dedicated credential provisioned`) — the cascade stops, loudly. Seeding a
    token without listing the subject silently restores the shared-token path. Both failure modes are
    a one-line edit away, and neither is visible in review.

    RENDERED WITH THE FLAG ON, because the flag is OFF by default and for a measured reason: turning
    it on refuses every mover, since the server-side expectation is only half the control. The catalog
    demands `service-token-<identity>` while the movers still PRESENT the shared APP_API_TOKEN, so
    every catalog call 401s and the cascade stops — driven live 2026-08-26 and rolled back. The
    remaining work is the CLIENT half: each privileged service reading its own token from the secret
    store and sending that. This invariant guards the halves that DO exist, so they cannot drift
    apart while that work is pending.
    """
    rendered = _helm_template("auth.dedicatedServiceCredentials=true")

    subjects: set[str] = set()
    for match in re.finditer(r'name:\s*\w*_?PRIVILEGED_SUBJECTS,\s*value:\s*"([^"]*)"', rendered):
        subjects |= {s.strip() for s in match.group(1).split(",") if s.strip()}
    assert subjects, "no *_PRIVILEGED_SUBJECTS is rendered at all — the credential binding is inert"

    seeded = set(re.findall(r"service-token-([A-Za-z0-9_-]+)=", rendered))
    assert seeded, "no service-token-<identity> is seeded — every privileged identity would be refused"

    missing_token = sorted(subjects - seeded)
    assert not missing_token, f"privileged identities with no dedicated credential seeded (each one is a fail-closed refusal): {missing_token}"

    orphan_token = sorted(seeded - subjects)
    assert not orphan_token, f"dedicated credentials seeded for identities that are not privileged, so they still take the shared-token path: {orphan_token}"


def test_the_scratch_emptyDir_is_BOUNDED() -> None:
    """An unbounded `/tmp` emptyDir makes one pod's upload every pod's problem.

    open_fastapi-audit, the disk half of the multipart finding. `lance.tmpVolume` rendered
    `emptyDir: {}`, and an emptyDir with no `sizeLimit` may grow until the NODE's filesystem is full.
    That matters here specifically because starlette spools each multipart file part to a
    `SpooledTemporaryFile` under `/tmp` — so this volume is exactly where an oversize or concurrent
    upload lands, and it is the same volume pyarrow/Lance spill and the OTel queue use.

    The blast radius is what makes it worth a gate rather than a comment: filling a node's disk gets
    every pod on that node evicted under DiskPressure, not just the one that took the upload. With a
    `sizeLimit`, the kubelet evicts the offending POD when it crosses the bound and the rest of the
    node is untouched.
    """
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    volume = re.search(r'define "lance\.tmpVolume".*?\{\{- end', helpers, re.DOTALL)
    assert volume is not None, "lance.tmpVolume helper vanished — this gate needs re-anchoring"
    assert "sizeLimit" in volume.group(0), (
        "the /tmp scratch emptyDir declares no sizeLimit, so a spooled multipart upload may grow until "
        "the NODE's disk is full and every pod on it is evicted under DiskPressure"
    )

    # And it must actually reach the rendered pods, not just the helper.
    docs = _rendered_docs()
    unbounded = [
        f"{doc['kind']}/{doc['metadata']['name']}"
        for doc in docs
        if doc.get("kind") in {"Deployment", "StatefulSet"}
        for vol in (doc["spec"]["template"]["spec"].get("volumes") or [])
        if vol.get("name") == "tmp" and not (vol.get("emptyDir") or {}).get("sizeLimit")
    ]
    assert not unbounded, f"workloads mount an unbounded /tmp emptyDir: {sorted(unbounded)}"


#: Every deployed app and the env var its own config reads for the docs opt-in. The names differ
#: because the services predate any shared setting; what must NOT differ is that each one is set.
_DOCS_ENV_BY_WORKLOAD: dict[str, str] = {
    "catalog": "LANCE_REST_DOCS",
    "lineage": "LINEAGE_DOCS",
    "medallion-producer": "MEDALLION_DOCS",
    "maintenance": "MAINTENANCE_DOCS",
    "viewer": "MEDIA_DOCS",
    "search": "MEDIA_DOCS",
    "annotator": "MEDIA_DOCS",
}


def _env_of(container: dict) -> dict[str, str]:
    return {e["name"]: str(e.get("value", "")) for e in (container.get("env") or []) if "name" in e}


def _app_containers(docs: list[dict]) -> dict[str, dict]:
    """Each workload's app container, keyed by the workload name it renders under."""
    found: dict[str, dict] = {}
    for doc in docs:
        if doc.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            env = _env_of(container)
            for workload, var in _DOCS_ENV_BY_WORKLOAD.items():
                if name.endswith(workload) and var in env:
                    found[workload] = container
    return found


def test_the_chart_TURNS_DOCS_OFF_by_default_and_ON_when_asked() -> None:
    """Interactive docs must be a deployment decision, and the deployment must actually make it.

    open_fastapi-audit — "/docs and /openapi.json are on in production for every served app".

    The code defaults are closed now, which is the load-bearing half. This is the other half, and it
    is the one the finding is really about: four services ALREADY carried a `docs_enabled` flag and
    every one of them shipped docs anyway, because no deployment path ever set it —
    `grep -rn DOCS chart/ .docker/ scripts/` matched nothing but an unrelated `_DOCS = _ROOT / "docs"`.
    A flag no manifest sets is not a control, it is a comment.

    So the gate asserts the env var is RENDERED, in both positions. Asserting only the "off" case
    would pass just as well against a chart that never mentions docs at all — which is exactly the
    state this finding describes.
    """
    # `explorer.enabled` is false by default (a fresh cluster must come up with no node
    # preparation), so viewer/search/annotator render only when asked for. Enable it here or the
    # gate silently covers four of the seven apps.
    _ON = "explorer.enabled=true"
    off = _app_containers(_rendered_docs(_ON))
    missing = sorted(set(_DOCS_ENV_BY_WORKLOAD) - set(off))
    assert not missing, (
        f"these workloads render no docs env var at all: {missing} — a service whose docs flag no manifest sets is one whose default nobody chose"
    )
    for workload, container in off.items():
        var = _DOCS_ENV_BY_WORKLOAD[workload]
        assert _env_of(container)[var].lower() in {"false", "0"}, f"{workload} renders {var}={_env_of(container)[var]} by default — docs are opt-in"

    on = _app_containers(_rendered_docs(_ON, "docs.enabled=true"))
    for workload in _DOCS_ENV_BY_WORKLOAD:
        var = _DOCS_ENV_BY_WORKLOAD[workload]
        value = _env_of(on[workload])[var]
        assert value.lower() in {"true", "1"}, f"docs.enabled=true left {workload} at {var}={value} — a knob that cannot open is a deletion"


#: Deployments this gate does NOT cover, enumerated from an actual render rather than guessed.
#: Matched as substrings of the rendered name, like the sibling gates in this file.
#:
#: Two different reasons, kept apart on purpose:
#:
#:  * SUBCHARTS — dapr, nats, openfga, dex, cloudnative-pg, openbao, rustfs, kueue, kuberay,
#:    greptimedb, perses, vmalert, alertmanager. Not ours to template, so not ours to harden.
#:  * INFRA PODS WE DO TEMPLATE — the OTel collector. Its securityContext is gated behind
#:    `security.infraContexts.enabled`, which values.yaml defaults OFF and stages explicitly:
#:    "both default OFF (behavior-identical); flip after the §7a live checks". That is a known,
#:    sequenced decision with its own live-check step, not an oversight, so this gate must not
#:    silently pre-empt it. It is listed HERE, visibly, rather than being missing from a
#:    hand-written first-party tuple where nobody could tell the difference.
_UNCOVERED_DEPLOYMENTS = (
    "dapr", "nats", "openfga", "dex", "cloudnative-pg", "openbao", "rustfs", "kueue",
    "kuberay", "greptimedb", "perses", "vmalert", "alertmanager",
    "otel-collector",
)  # fmt: skip


def _first_party_deployments(docs: list[dict]) -> list[dict]:
    """Every rendered Deployment that is OURS, derived rather than listed.

    The gate this replaces carried a hand-written tuple of ten names, and the cost of that is not
    hypothetical: `compute`, `controlplane`, `flows` and `ingest` were simply absent from it, so the
    hardening gate skipped four first-party pods silently — a new Deployment was covered only if
    somebody remembered to add it. Deriving the set means the default is COVERED and an exemption has
    to be argued for by name.
    """
    return [doc for doc in docs if doc.get("kind") == "Deployment" and not any(skip in doc["metadata"]["name"] for skip in _UNCOVERED_DEPLOYMENTS)]


def test_every_first_party_container_carries_the_HARDENING_the_chart_claims() -> None:
    """`values.yaml` says pod hardening is "applied to every app container via the lance.securityContext
    helper". For six Deployments that sentence was false.

    open_fastapi-audit — "Six first-party fleet Deployments render with no container securityContext at
    all". `grep -n securityContext chart/templates/fleet.yaml chart/templates/controlplane.yaml`
    returned NOTHING, so rask-{gateway,compute,controlplane,flows,ingest,notifications} rendered with
    none of it, while every lance-plane and web Deployment called the helper.

    WHAT IS AND IS NOT AT STAKE, because the finding is careful about this and so should the gate be.
    `.docker/gateway.dockerfile` ends `USER app`, and a container that never runs as uid 0 holds no
    effective capabilities anyway — so the missing `drop: ["ALL"]` was nominal rather than an open
    door. What genuinely was missing: admission-time `runAsNonRoot` (the image's USER is a promise the
    image makes; this is the one the CLUSTER enforces), a RuntimeDefault seccomp profile, and a
    writable root filesystem on the pod the Ingress routes `/api` to. Blast-radius hardening, on the
    most exposed surface in the release.

    The gate also derives its own subject list — see `_first_party_deployments`. The version it
    replaces named ten Deployments by hand and silently skipped the four fleet ones that had no
    hardening, which is how this survived a gate that existed to catch it.
    """
    docs = _rendered_docs()
    required = ("runAsNonRoot", "seccompProfile", "allowPrivilegeEscalation", "readOnlyRootFilesystem")

    unhardened: dict[str, list[str]] = {}
    for doc in _first_party_deployments(docs):
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            context = container.get("securityContext") or {}
            missing = [key for key in required if key not in context]
            if missing:
                unhardened[f"{doc['metadata']['name']}/{container['name']}"] = missing

    assert not unhardened, f"first-party containers render without the hardening values.yaml claims they all get: {unhardened}"


def test_a_read_only_rootfs_is_SURVIVABLE_on_every_first_party_pod() -> None:
    """Hardening that breaks the pod is not hardening, it is a rollback waiting to happen.

    `readOnlyRootFilesystem: true` is the chart default, and these images write to /tmp for OTel and
    pyarrow exactly as the lance ones do. The scratch pair exists for this (`lance.tmpMount` +
    `lance.tmpVolume`, bounded by `security.tmpSizeLimit`); a pod that gets the securityContext
    without the volume crash-loops on its first spill instead of on the next deploy.
    """
    naked: list[str] = []
    for doc in _first_party_deployments(_rendered_docs()):
        spec = doc["spec"]["template"]["spec"]
        volumes = {vol["name"] for vol in (spec.get("volumes") or [])}
        for container in spec.get("containers") or []:
            if not (container.get("securityContext") or {}).get("readOnlyRootFilesystem"):
                continue
            mounts = {m["mountPath"] for m in (container.get("volumeMounts") or [])}
            if "/tmp" not in mounts or "tmp" not in volumes:
                naked.append(f"{doc['metadata']['name']}/{container['name']}")

    assert not naked, f"read-only rootfs with no writable /tmp: {sorted(naked)}"


#: The calls that CREATE a first-party metric. A module containing any of these emits a series the
#: estate owns, whatever the file is named.
_INSTRUMENT_FACTORIES = ("create_counter", "create_histogram", "create_observable_gauge", "create_gauge", "create_up_down_counter")


def _modules_declaring_instruments() -> dict[str, list[str]]:
    """Every repo module that creates a metric, keyed by the series names it declares.

    DISCOVERED, not assumed to live in a file called `metrics.py`. That assumption is what made the
    `lance-catalog` misclassification possible: the catalog's `catalog_writes_shed_total` counter is
    created in `api/load_shed.py`, so a grep for `metrics.py` found nothing and concluded the group
    rode shared HTTP-server series instead — a claim that was false in both halves.
    """
    found: dict[str, list[str]] = {}
    for path in (*(REPO / "services").rglob("*.py"), *(REPO / "packages").rglob("*.py")):
        if "/tests/" in str(path):
            continue
        text = path.read_text(errors="ignore")
        if not any(factory in text for factory in _INSTRUMENT_FACTORIES):
            continue
        # The SAME translation the forward gate applies, and it has to be: the instrument is named
        # `catalog.writes.shed` while the series is `catalog_writes_shed_total`. Matching the raw
        # instrument name against PromQL would find nothing and report the group as clean — which is
        # how the first version of this gate passed against the very misclassification it exists to
        # catch.
        series: list[str] = []
        for name, tail in re.findall(r'create_(?:counter|up_down_counter|histogram|observable_gauge|gauge)\(\s*"([^"]+)"(.*?)\)', text, re.DOTALL):
            base = name.replace(".", "_")
            unit = re.search(r'unit\s*=\s*"([^"]*)"', tail)
            suffix = _UCUM_SUFFIX.get(unit.group(1)) if unit else None
            base = f"{base}_{suffix}" if suffix else base
            series += [base, f"{base}_total"]
        if series:
            found[str(path.relative_to(REPO))] = series
    return found


def test_no_group_is_called_THIRD_PARTY_while_a_first_party_instrument_emits_it() -> None:
    """An entry in `_THIRD_PARTY_ALERT_GROUPS` is a claim, and a false one disables a gate.

    open_fastapi-audit — "`_THIRD_PARTY_ALERT_GROUPS[\"lance-catalog\"]` is a false claim, and it
    exempts the catalog's only alert from the phantom-metric gate".

    That entry said the catalog "ships no metrics.py — its rules ride the shared HTTP server metrics".
    Both halves were false: `catalog_writes_shed_total` is created by `_meter.create_counter` in
    `api/load_shed.py`, and no `lance-catalog` rule references any `http_server_*` series. The cost is
    precisely what `test_every_first_party_ALERT_names_a_metric_the_service_actually_EMITS` exists to
    prevent — being in the third-party map means that parametrized gate SKIPS the group, so a typo in
    the one rule guarding the catalog's write-capacity ceiling would pass `promtool check rules`,
    `promtool test rules` and every chart invariant, and simply never fire.

    So the classification itself is now gated. A group may only be declared third-party if no module
    in this repo creates the series it queries — discovered by the instrument FACTORY CALL rather than
    by filename, so the next first-party instrument outside a `metrics.py` is not silently reclassified
    the same way.
    """
    rules = yaml.safe_load((REPO / "chart/alerting/rules.yml").read_text())
    emitted = {name: module for module, names in _modules_declaring_instruments().items() for name in names}

    misclassified: dict[str, list[str]] = {}
    for group in rules["groups"]:
        if group["name"] not in _THIRD_PARTY_ALERT_GROUPS:
            continue
        referenced = " ".join(rule.get("expr", "") for rule in group.get("rules") or [])
        ours = sorted({f"{name} ({module})" for name, module in emitted.items() if name in referenced})
        if ours:
            misclassified[group["name"]] = ours

    assert not misclassified, (
        f"these groups are declared third-party but query series this repo instruments: {misclassified}. "
        f"A third-party entry exempts the group from the phantom-metric gate, so a typo in its rules "
        f"would never be caught and the alert would silently never fire"
    )


# ---------------------------------------------------------------------------------------------
# `runAsNonRoot` CANNOT VERIFY A NAMED USER, so an image the chart hardens must declare a numeric one.
#
# `lance.securityContext` sets `runAsNonRoot: true` on every first-party app container, and its own
# comment says that "enforces the image's non-root USER (catalog uid 10001 …) at admission". It does
# not. The kubelet compares a NUMBER against 0, so an image ending `USER app` is refused outright:
#
#   Error: container has runAsNonRoot and image has non-numeric user (app),
#          cannot verify user is non-root
#
# Measured on the k3s estate 2026-08-27, when a helm upgrade first applied that hardening to six
# fleet containers: gateway, compute, controlplane, flows, ingest and notifications all wedged in
# `CreateContainerConfigError` while their previous pods kept serving — so nothing went down, nothing
# alerted, and the six deployments simply stopped being able to roll. The images that were ALREADY
# hardened (rest-catalog, frontend, assist-runner) end `USER 10001` and were unaffected, which is the
# whole tell: the six dockerfiles create their user with `useradd --uid 10001 app` and then name it.
#
# The fix belongs in the dockerfile rather than the chart: `lance.securityContext` is shared by images
# with DIFFERENT users (the web images run `bun`), so a blanket `runAsUser` in the helper would be
# wrong for some of them, while a numeric `USER` is correct for every image on its own terms — and it
# is the estate's own dockerfile rule (non-root UID >= 10000).
# ---------------------------------------------------------------------------------------------

_DOCKER_DIR = REPO / ".docker"
#: `USER root` is legitimate for an image the chart does not harden (a Postgres extension build).
_ROOT_BY_DESIGN = {"cnpg-age-ext.dockerfile"}


def _final_user(dockerfile: Path) -> str | None:
    """The last `USER` a dockerfile declares — the one the container actually runs as."""
    users = re.findall(r"^USER\s+(\S+)", dockerfile.read_text(encoding="utf-8"), re.MULTILINE)
    return users[-1] if users else None


def test_an_image_the_chart_hardens_declares_a_NUMERIC_user() -> None:
    """A named `USER` plus `runAsNonRoot` is a container that cannot start."""
    offenders: list[str] = []
    for dockerfile in sorted(_DOCKER_DIR.glob("*.dockerfile")):
        if dockerfile.name in _ROOT_BY_DESIGN:
            continue
        user = _final_user(dockerfile)
        if user is None or user.isdigit():
            continue
        offenders.append(f"{dockerfile.name} ends `USER {user}`")

    assert not offenders, (
        "`lance.securityContext` sets runAsNonRoot on every first-party app container, and the kubelet "
        "refuses a non-numeric user with `cannot verify user is non-root` — the container never starts, "
        "the old pod keeps serving, and the deployment silently stops being able to roll:\n  "
        + "\n  ".join(offenders)
        + "\nDeclare the uid the dockerfile already creates (e.g. `USER 10001`), not its name."
    )


def test_the_producer_can_reach_the_catalog_regardless_of_the_quality_review_flag() -> None:
    """The cascade HEAD must be able to register what it writes on every shipped configuration.

    `MEDALLION_CATALOG_URL` / `_ROOT` / `_SERVICE_IDENTITY` used to render only inside the
    `medallion.qualityReview` conditional, and that flag defaults FALSE (`chart/values.yaml`) and is
    unset in values-local, values-prod and values-live-pins. So on the shipped chart the producer had no
    catalog address, `produce.py`'s registration branch read an empty `settings.catalog_url` and skipped
    SILENTLY, and `POST /produce` wrote a bronze dataset with no `table:` object — no FGA ownership
    tuple, `policy/set` 404, no `_protection/` record reachable, no grant able to name it. Nothing went
    red, which is the whole problem.

    This is the second instance of the shape. The first is recorded on the mover env block in the same
    file ("Governance belongs to the cascade, so the catalog's address does too: unconditional, on every
    mover") — and the producer, which is the cascade's head, was left behind. Gating a service's ability
    to register what it writes on an unrelated feature flag is how a tier becomes ungovernable while
    every gate stays green.
    """
    import yaml

    for review in ("false", "true"):
        rendered = _helm_template(f"medallion.qualityReview={review}")
        producer = next(
            (doc for doc in yaml.safe_load_all(rendered) if doc and doc.get("kind") == "Deployment" and "medallion-producer" in doc["metadata"]["name"]),
            None,
        )
        assert producer is not None, f"the medallion producer did not render at qualityReview={review}"
        env = {item["name"] for item in producer["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        missing = {"MEDALLION_CATALOG_URL", "MEDALLION_CATALOG_ROOT", "MEDALLION_CATALOG_SERVICE_IDENTITY"} - env
        assert not missing, (
            f"at medallion.qualityReview={review} the producer cannot reach the catalog: {sorted(missing)} "
            "not rendered. Its bronze writes would carry no table record, so no policy, protection or "
            "grant could ever name them — and the registration code skips silently when the URL is empty."
        )


def test_the_media_head_can_register_the_bronze_it_lands() -> None:
    """`/ingest-media` must be able to govern its own tier on every shipped configuration.

    The catalog ADDRESS is the first half and the test above pins it — `/ingest-media` is served by the
    same producer pod as `/produce`, so it inherits that fix. The second half is unique to this lane and
    invisible from the template: `register_table` addresses only paths INSIDE the root the catalog is
    connected to, so `MEDALLION_MEDIA_BRONZE_URI` must resolve under `MEDALLION_CATALOG_ROOT` or the
    media head fails closed on every call. The two are rendered from DIFFERENT expressions —
    `lance.stageBucket` honours the `medallion.buckets` zoning map for the media namespace, while the
    catalog root is `rustfs.bucket` flat — so zoning `bronze-media` into its own bucket would make the
    ingest door 503 with nothing in the chart looking wrong. `relative_location` is the exact seam the
    producer uses, so this asserts registerability rather than a string resemblance.
    """
    import yaml

    from medallion.services.catalog_register import RegisterError, relative_location

    for review in ("false", "true"):
        rendered = _helm_template(f"medallion.qualityReview={review}")
        producer = next(
            (doc for doc in yaml.safe_load_all(rendered) if doc and doc.get("kind") == "Deployment" and "medallion-producer" in doc["metadata"]["name"]),
            None,
        )
        assert producer is not None, f"the medallion producer did not render at qualityReview={review}"
        env = {item["name"]: item.get("value", "") for item in producer["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        missing = {"MEDALLION_MEDIA_BRONZE_URI", "MEDALLION_CATALOG_ROOT", "MEDALLION_CATALOG_URL"} - set(env)
        assert not missing, f"at medallion.qualityReview={review} the media head cannot register what it lands: {sorted(missing)} not rendered"
        try:
            location = relative_location(env["MEDALLION_MEDIA_BRONZE_URI"], env["MEDALLION_CATALOG_ROOT"])
        except RegisterError as exc:  # noqa: PERF203 — one render per flag state, not a hot loop
            pytest.fail(
                f"at medallion.qualityReview={review} the shipped chart lands media bronze somewhere the catalog cannot name, "
                f"so POST /ingest-media fails closed on every call: {exc}"
            )
        assert location, "the media bronze URI resolved to the catalog root itself — a tier must have its own location"


def test_the_ingest_pod_gets_the_SAME_external_blob_bases_as_the_catalog() -> None:
    """One operator decision, rendered at both doors that enforce it.

    `LANCE_EXTERNAL_BLOB_BASES` was rendered on the CATALOG deployment only, so on every shipped
    chart `approved_external_base()` returned None in the ingest pod and EVERY ingest COPIED the
    corpus into the lakehouse instead of referencing it. The repo carries the measurement of what
    that costs: 3,232 bytes external against 4,002,901 bytes managed on a 4 MB corpus
    (`services/ingest/src/ingest/adapters.py`). The only signal was one warning log; the run was green.

    The ingest module's own docstring already stated the rule — "Deliberately shares the catalog's
    variable name: in-cluster the same operator decision has to hold at both doors" — while the chart
    made it impossible to hold.

    RENDERED FROM THE ONE `vending.externalBlobBases` VALUE, never duplicated into a values `env:`
    map. Two literals is how the ingest side approves a write under a base the catalog's manifest did
    not register, and `initial_bases` is create-mode only — so that divergence is unrepairable on
    every table it lands on.
    """
    rendered = _helm_template("vending.externalBlobBases=s3://probe-bucket/blobs/")

    carriers = set()
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for entry in container.get("env") or []:
                if entry.get("name") == "LANCE_EXTERNAL_BLOB_BASES" and entry.get("value") == "s3://probe-bucket/blobs/":
                    carriers.add(doc["metadata"]["name"])

    assert any("ingest" in name for name in carriers), (
        f"the ingest deployment does not carry LANCE_EXTERNAL_BLOB_BASES, so every ingest copies the "
        f"corpus instead of referencing it; deployments that do carry it: {sorted(carriers)}"
    )


def _env_by_component(docs: list[dict], component: str) -> dict[str, str]:
    """The merged container env of the Deployment carrying ``app.kubernetes.io/component: <component>``.

    Selected by LABEL rather than by name because `_helm_template` renders without a release name, so a
    name-prefix match would encode helm's `release-name` placeholder.
    """
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        labels = ((doc["spec"]["template"].get("metadata") or {}).get("labels")) or {}
        if labels.get("app.kubernetes.io/component") != component:
            continue
        env: dict[str, str] = {}
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            env |= _env_of(container)
        return env
    return {}


def test_no_workload_references_a_secret_the_render_does_not_create() -> None:
    """A `secretKeyRef` to a Secret nothing creates is a pod that never starts, and a GREEN render.

    THE FAILURE THIS PINS. `dapr.sidecars=false` is a documented, supported toggle, and
    `services.yaml`'s own fail message tells an operator to pair it with `catalog.controlEmit=false`.
    That pair rendered cleanly — and left THIRTEEN Deployments (all seven zones, maintenance, the
    producer, three movers and lineage) carrying a reference to `-dapr-app-token`, which is gated on
    `dapr.sidecars` and therefore absent. Each fails with CreateContainerConfigError; nothing in the
    render says why.

    The cause was one Secret doing double duty under two different gates: the Dapr app token
    (`dapr.sidecars`) and `LINEAGE_SERVICE_TOKEN`, the service credential for reading lineage
    (`auth.enabled`).

    Checked across the TOGGLE COMBINATIONS, not just the default render, because the default is the one
    configuration this class cannot appear in — every consumer and its Secret are on together there.
    """
    combinations = [
        ("default", []),
        ("sidecars off", ["dapr.sidecars=false", "catalog.controlEmit=false"]),
        ("auth off", ["auth.enabled=false"]),
        ("both off", ["dapr.sidecars=false", "catalog.controlEmit=false", "auth.enabled=false"]),
    ]
    problems: list[str] = []
    for label, extra in combinations:
        docs = _rendered_docs(*extra)
        secrets = {d["metadata"]["name"] for d in docs if d.get("kind") == "Secret"}
        # SUBCHART NAMING is a separate concern and not this gate's. A subchart names its workloads
        # `{{ .Release.Name }}-x` while this chart's own Secrets use `lance.fullname`, so the two agree
        # only when the release is named `rask` — which it always is here, and which `_helm_template`
        # does not pin. Filtered rather than asserted, so this gate reports the class it was built for
        # instead of a rendering artefact; the naming mismatch is recorded in the backlog.
        secrets |= {name.replace("release-name-", "rask-", 1) for name in secrets}
        for doc in docs:
            for name in _secret_refs(doc):
                # A Dapr Component's secretKeyRef names a key in the SECRET STORE (OpenBao), not a
                # Kubernetes Secret — a different namespace of names entirely.
                if doc.get("kind") == "Component" or name in secrets:
                    continue
                problems.append(f"[{label}] {doc.get('kind')}/{doc['metadata']['name']} -> missing Secret {name!r}")
    assert not problems, "workloads reference Secrets the render never creates:\n  " + "\n  ".join(sorted(set(problems)))


def _secret_refs(doc: object) -> list[str]:
    """Every `secretKeyRef` name anywhere in a rendered document."""
    found: list[str] = []
    if isinstance(doc, dict):
        ref = doc.get("secretKeyRef")
        if isinstance(ref, dict) and ref.get("name"):
            found.append(str(ref["name"]))
        for value in doc.values():
            found.extend(_secret_refs(value))
    elif isinstance(doc, list):
        for value in doc:
            found.extend(_secret_refs(value))
    return found


def test_the_MAINTENANCE_service_stages_its_lineage_the_same_way_the_catalog_does() -> None:
    """The sibling of the catalog test below, and it was the same defect on a second service.

    `services/maintenance` builds a lineage emitter with `outbox_uri=settings.lineage_outbox_uri`
    (`service.py`), whose alias is `MAINTENANCE_LINEAGE_OUTBOX_URI` — and the chart rendered nothing, so
    the emitter fell back to a plain publish exactly as the catalog's did.

    Cheaper to lose than a write announcement, and still worth staging: a maintenance run is only
    emitted when the sweep does MATERIAL work (`sweep.py::_did_material_work` gates on
    `fragments_removed or old_versions_removed`), so a dropped publish loses the record of the one tick
    that actually rewrote bytes, and the graph then shows a dataset whose files changed with nothing
    saying what changed them.

    Same prefix as the catalog and the movers, for the same reason: `lineage/api/reconcile_cron.py` is
    the only thing that drains it. Found because the agent that wired the catalog said plainly that it
    had left this one — an honest `left_undone` is what turned a second silent hole into a test.
    """
    docs = _rendered_docs()
    env = _env_by_component(docs, "maintenance")
    lineage_env = _env_by_component(docs, "lineage")
    assert env, "no maintenance Deployment rendered"

    staged = env.get("MAINTENANCE_LINEAGE_OUTBOX_URI", "")
    assert staged, (
        "the maintenance Deployment renders no MAINTENANCE_LINEAGE_OUTBOX_URI, so its lineage emitter "
        "degrades to a plain publish and a bus blip loses the record of the sweep tick that rewrote bytes"
    )
    drained = lineage_env.get("LINEAGE_OUTBOX_URI", "")
    assert staged == drained, f"maintenance stages into {staged!r} but the relay drains {drained!r} — nothing would ever collect it"


def test_the_catalog_STAGES_its_write_announcement_into_the_prefix_the_relay_DRAINS() -> None:
    """The catalog's lineage outbox must be rendered, and must name the prefix the lineage relay reads.

    `LANCE_LINEAGE_OUTBOX_URI` exists in `catalog/core/config.py` and is threaded all the way to
    `outbox.publish_lineage_with_outbox` in `lineage_emit.py` — and rendered NOWHERE, so on every shipped
    chart the catalog's emit degraded to the pre-#4 plain publish. This is the same failure class as the
    `MEDALLION_LINEAGE_OUTBOX_URI` dead-env above, mirrored: there the chart set what no code read, here
    the code reads what no chart sets. Only a RENDER can tell either of them apart from a working feature.

    What it costs on the catalog specifically: the emit is inline-awaited and best-effort AFTER the Lance
    write commits, and medallion's `/bronze-arrival` subscription reacts to that announcement — so a lost
    publish does not merely under-report provenance, the whole bronze->silver->gold run silently never
    happens.

    THE PREFIX EQUALITY IS THE LOAD-BEARING HALF. Staging is only durable because
    `lineage/api/reconcile_cron.py` drains `LINEAGE_OUTBOX_URI` and re-publishes what it finds; a catalog
    staging anywhere else would write objects that nothing on the estate ever reads, which looks exactly
    like durability and is not.
    """
    docs = _rendered_docs()
    catalog_env = _env_by_component(docs, "catalog")
    lineage_env = _env_by_component(docs, "lineage")
    assert catalog_env, "no catalog Deployment rendered"

    staged = catalog_env.get("LANCE_LINEAGE_OUTBOX_URI", "")
    assert staged, (
        "the catalog Deployment renders no LANCE_LINEAGE_OUTBOX_URI, so `publish_lineage_with_outbox` "
        "degrades to a plain publish: a crash between the Lance commit and the publish loses the write "
        "announcement, and with it the bronze->silver->gold run that announcement triggers"
    )
    drained = lineage_env.get("LINEAGE_OUTBOX_URI", "")
    assert staged == drained, (
        f"the catalog stages to {staged!r} but the lineage relay drains {drained!r} — staged events would "
        "sit in a prefix nothing re-ingests or re-publishes, which is indistinguishable from durability "
        "until an outage"
    )

    # The pair is ONE mechanism behind ONE switch: staging with the drain off leaves objects nothing
    # collects, so an operator turning the chain off must turn off both halves, not one.
    off = _env_by_component(_rendered_docs("services.lineage.outbox.enabled=false"), "catalog")
    assert "LANCE_LINEAGE_OUTBOX_URI" not in off, "services.lineage.outbox.enabled=false must also stop the catalog staging (the drain is off with it)"
