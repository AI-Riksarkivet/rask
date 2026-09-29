"""Ray emits no spans, on either of its two independent tracing switches.

Ray has TWO tracing planes and they are configured separately, which is the trap:

  * Ray CORE — `headGroupSpec.rayStartParams: {tracing-startup-hook: "module:function"}`. Gives
    PRODUCER/CONSUMER spans on `.remote()` calls. Upstream documents it as Alpha and "no longer under
    active development", and Ray Data / Train / Tune contribute ZERO spans of their own, so the payoff
    is generic per-task spans — worth having, not worth building a trace story on.
  * Ray SERVE — `RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH`. Gives proxy -> router -> replica spans and,
    crucially, HONOURS AN INBOUND `traceparent`. That is the segment that joins a gateway-originated
    trace to the model call, and it is the higher-value of the two. Ray's own monitoring docs never
    mention it exists.

The two hooks have DIFFERENT CONTRACTS and must not be copy-pasted from one another: the core hook
takes no arguments and returns None (it sets the provider itself); the Serve hook takes no arguments
and RETURNS a list of SpanProcessor (Serve builds the provider). Wiring one into the other's env var
fails soft — Serve catches a bad import, logs, and keeps serving — so the failure mode is silence,
not an error.

Until now the only OTel code in any of the nine runners was `runners/htr`'s own `_init_otel`, which
built a TracerProvider that produced ZERO spans (no span is opened anywhere under runners/) and
defaulted its service name to a workload. One modality's private, inert telemetry lane inside a
sealed environment.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path


def _module_tree(module_name: str) -> ast.Module:
    """The parsed AST of an importable module.

    `module.__file__` is `str | None` (a namespace package has none), so it is narrowed rather than
    ignored — `ty` is right to reject `Path(str | None)`, and a `  # ` here would be the
    wrong tool's syntax for a real hole.
    """
    module = __import__(module_name, fromlist=["x"])
    source_path = module.__file__
    assert source_path is not None, f"{module_name} has no source file to inspect"
    return ast.parse(Path(source_path).read_text(encoding="utf-8"))


def _code_identifiers(module_name: str) -> set[str]:
    """Every NAME and ATTRIBUTE the module's code references — docstrings and comments excluded.

    Matching raw source text is the wrong tool here and this file learned it the hard way: the module
    under test DOCUMENTS why `SimpleSpanProcessor` and `get_runtime_context` must not be used, so a
    substring check fails on the very prose that explains the rule. `ast` sees code only.
    """
    tree = _module_tree(module_name)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", maxsplit=1)[-1])
            if node.asname:
                names.add(node.asname)
    return names


def test_the_two_hooks_have_DIFFERENT_contracts() -> None:
    """The copy-paste trap. Core returns None and sets the provider; Serve returns processors and lets
    Serve set it. Both take zero arguments — Ray calls them by import path with no args."""
    from service_kit.ray_tracing import serve_span_processors, setup_tracing

    assert inspect.signature(setup_tracing).parameters == {}, "the core hook must take no arguments"
    assert inspect.signature(serve_span_processors).parameters == {}, "the Serve hook must take no arguments"

    processors = serve_span_processors()
    assert isinstance(processors, list), f"the Serve hook must RETURN a list of SpanProcessor, got {type(processors)}"
    assert processors, "an empty processor list means Serve exports nothing, silently"


def test_it_uses_a_BATCH_processor_never_a_SIMPLE_one() -> None:
    """Ray opens a span on EVERY `.remote()`. Both hooks Ray ships in its own docs use
    SimpleSpanProcessor, which exports synchronously — that puts an HTTP round-trip on the
    task-submission hot path of a batch system."""
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from service_kit.ray_tracing import serve_span_processors

    assert all(isinstance(p, BatchSpanProcessor) for p in serve_span_processors())

    assert "SimpleSpanProcessor" not in _code_identifiers("service_kit.ray_tracing"), "SimpleSpanProcessor puts an export round-trip on every .remote()"


def test_setup_tracing_does_not_stomp_an_existing_sdk_provider() -> None:
    """Idempotent by design: a Ray job script may already have built its own provider (the
    medallion stage/train jobs do, to continue the submitter's trace), and OTel refuses a second
    set_tracer_provider with a warning rather than an error — so a second call would silently leave
    the first provider in place while the caller believed otherwise."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    from service_kit.ray_tracing import setup_tracing

    trace.set_tracer_provider(TracerProvider())
    before = trace.get_tracer_provider()

    setup_tracing()

    assert trace.get_tracer_provider() is before, "setup_tracing replaced a provider that was already installed"
