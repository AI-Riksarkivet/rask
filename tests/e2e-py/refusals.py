"""What a refusal owes the caller it refuses, held on every live leg that is refused ([[LH-255]]).

A stock client dispatches on the spec's error code, which every implementation shares, and an HTTP door's status is only
that code's mapping (`lance_docs/ns_catalog/namespace/operations/errors.md`, `supported-catalogs/lance-rest.md` § Error
Code to HTTP Status Mapping), so a refusal is the status and the problem body's code together. A 403 that still wrote
answers both, so the leg reads what the door could change before the call and again after it: the table as its owner
describes it and the tuples the store holds on it (`fga_store`). And the body, whose `detail` the spec lets carry a stack
trace, names nothing the caller did not send: not the owner, not where the bytes live, no uuid, not another tenant.

Criterion 2's matrix (`test_the_catalog_is_correct.py`) sends each cell before the cell's verdict is known, so it reads its
answer itself, and judges a refused one through the same `body_of`, `problem_code` and `names_in`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import requests
from lance_namespace import ErrorCode


def body_of(answer: requests.Response) -> str:
    """``answer``'s body as the spec writes it: JSON is UTF-8 (RFC 8259), which `requests` does not assume for
    `application/problem+json`."""
    return answer.content.decode("utf-8", errors="replace")


def problem_code(text: str) -> int | None:
    """The spec code a problem+json body carries."""
    try:
        body = json.loads(text)
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, int) else None


def names_in(text: str, names: Iterable[str]) -> list[str]:
    """The ``names`` a body carries, its JSON escapes resolved, so a name is found however the server encoded it."""
    try:
        read = json.dumps(json.loads(text), ensure_ascii=False)
    except ValueError:
        read = text
    return sorted(name for name in names if name and name in read)


def described(catalog: str, table: str, headers: Mapping[str, str]) -> dict[str, Any]:
    """``table`` as its describe answers ``headers``' bearer, with no credential vended: a vend is a session of its own on
    every call, so a reading compared before and after another call leaves it out."""
    answer = requests.post(f"{catalog}/v1/table/{table}/describe", headers=headers, json={"vend_credentials": False}, timeout=30)
    assert answer.status_code == 200, f"describe {table} -> {answer.status_code}: {body_of(answer)[:500]}"
    return answer.json()


def namespace_uuid(catalog: str, namespace: str, headers: Mapping[str, str]) -> str:
    """The uuid the catalog minted for ``namespace`` ([[LH-519]]), as its describe answers ``headers``' bearer."""
    answer = requests.post(f"{catalog}/v1/namespace/{namespace}/describe", headers=headers, json={}, timeout=30)
    assert answer.status_code == 200, f"describe {namespace} -> {answer.status_code}: {body_of(answer)[:500]}"
    return str(answer.json()["properties"]["rask.uuid"])


def assert_refused(
    send: Callable[[], requests.Response],
    *,
    status: int,
    code: ErrorCode,
    unchanged: Callable[[], object],
    foreign: Iterable[str],
) -> None:
    """Send a request its caller may not make, and hold its answer to a refusal: ``status`` with the spec's ``code``,
    what ``unchanged`` reads the same after the call as before it, and no name of ``foreign`` in the body."""
    before = unchanged()
    answer = send()
    body = body_of(answer)
    said = f"{answer.request.method} {answer.request.url} -> {answer.status_code}: {body[:500]}"
    assert (answer.status_code, problem_code(body)) == (status, code), said
    assert unchanged() == before, f"the refused call changed what it acts on: {said}"
    leaked = names_in(body, foreign)
    assert not leaked, f"the refusal names {leaked}, which its caller did not send: {said}"
