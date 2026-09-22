"""The shared app token is read through ONE resolver, inbound and outbound alike.

`service_kit.governed.dapr_auth.expected_app_token()` returns the Dapr secret store's value when
`RASK_APP_TOKEN_FROM_STORE` is set and the env value otherwise. Its own docstring names three
consumers, and all three are INBOUND doors — the callers that PRESENT the token were never part of
that "one resolver" claim, and each one that read `settings.app_api_token` directly disagreed with it
on exactly the deployments the estate's secrets rule produces. Measured live 2026-09-22: eleven of
eleven fleet deployments carry `RASK_APP_TOKEN_FROM_STORE=true`.

THE FAILURE IS SILENT BY CONSTRUCTION, three times over now. A credential builder that gets `None`
sends NO headers rather than half a pair, so the refusal happens one service away and the caller
reports a generic failure or nothing at all:

  * `medallion` — 2,700 catalog calls in twenty-five minutes with zero successes.
  * `notifications` — `POST /notifications-reconcile-cron` answered **500 3,087 times**, ~9.6 per five
    minutes, each an unhandled 401 from `invoke/lineage/method/events`. Its reconciler exists because
    the bus alone is provably incomplete, so what was lost is not a retry: it is every event only the
    durable walk could have caught, on a Ready pod with a healthy bus lane beside it.
  * `ingest` — not live, because it is privileged at both doors and its dedicated resolver answers
    first. It becomes live the moment that path is turned off while the token stays in the store.

WHY A GATE AND NOT THREE FIXES. The first two were fixed three days apart, each as its own incident,
and the third was still there. The class is "a second accessor for one secret", and the only thing
that ends it is a rule a new caller cannot write around by accident.

THE RULE: a production read of `app_api_token` must sit in a function that also reaches
`expected_app_token`. That permits the env value as a FALLBACK behind the resolver — which is what
every fixed site does — and refuses it as the sole source.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]

#: `dapr_auth` DEFINES the setting and the resolver, so it reads the field by definition.
OWNER = "packages/service-kit/src/service_kit/governed/dapr_auth.py"

#: The resolver a reader must reach for. Matched by NAME rather than by import, because every fixed
#: site imports it inside the function (the module is imported late to keep settings import-light).
RESOLVER = "expected_app_token"


def _names_read(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every attribute and bare name the function mentions."""
    seen: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute):
            seen.add(node.attr)
        elif isinstance(node, ast.Name):
            seen.add(node.id)
    return seen


def _unresolved_readers() -> list[str]:
    """`file:line in func()` for every production function reading the field without the resolver."""
    offenders: list[str] = []
    for root in (REPO / "services", REPO / "packages"):
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if "/tests/" in rel or rel == OWNER:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for func in ast.walk(tree):
                if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                names = _names_read(func)
                if "app_api_token" in names and RESOLVER not in names:
                    offenders.append(f"{rel}:{func.lineno} in {func.name}()")
    return offenders


def test_no_outbound_credential_reads_the_token_off_settings_alone() -> None:
    """Exhaustive, not a sample: ONE such reader is a service whose every call goes out unauthenticated
    on a store-path deployment, and says nothing about it."""
    offenders = _unresolved_readers()
    assert not offenders, (
        f"these read `app_api_token` without going through `{RESOLVER}`: {offenders}. On a deployment "
        "with `RASK_APP_TOKEN_FROM_STORE=true` — which is all eleven of them — that field is empty, so "
        "the credential builder sends no headers and the refusal surfaces a service away, or not at all"
    )


def test_the_walk_can_see_the_readers_it_is_guarding() -> None:
    """A walk that matched nothing would pass in perfect silence — the failure mode this estate has
    shipped a gate with before. There are known compliant readers; find them."""
    compliant = 0
    for root in (REPO / "services", REPO / "packages"):
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if "/tests/" in rel or rel == OWNER:
                continue
            for func in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                names = _names_read(func)
                if "app_api_token" in names and RESOLVER in names:
                    compliant += 1
    assert compliant >= 4, (
        f"only {compliant} compliant readers found; medallion (x2), notifications and ingest all carry "
        "this shape, so a lower count means the walk is broken rather than the estate clean"
    )
