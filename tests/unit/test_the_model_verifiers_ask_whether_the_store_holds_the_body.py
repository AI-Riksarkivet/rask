"""The two model verifiers ask whether the store the estate uses HOLDS this checkout's model.

[[LH-201]]. A service checks against the model its own image carries, found in the store's history by
its canonical body, so the store's NEWEST model is not authoritative for anything: a body held below
it (an older image, a revert, a writer that wrote since) is the one every pod built from this checkout
resolves, and the hook writes only a body the store lacks. `scripts/fga-store-check.sh` and the
deployed-model e2e check report whether the store took this checkout's model, so they ask exactly that
of the store the estate uses: the one `RASK_FGA_STORE_ID` pins, else the newest named `lance-catalog`
(`fga.newest_store`). Held at any depth passes and says where; absent fails and names the relation diff
against the newest.

DRIVEN, NOT READ. Each verifier runs for real against a stub OpenFGA that serves one model per page, so
a verifier that reads only the first page cannot find an older one. The stub lists the store the
estate uses SECOND, behind an older `lance-catalog`: the boot-race double-create `scripts/e2e_stack.sh`
records from CI, and the shape in which `stores[0]` is the wrong store.

`fga-store-check.sh` runs with `kubectl` faked onto PATH, and the fake runs the in-pod half under an
interpreter with no site-packages. The catalog image lags the checkout in exactly the state the check
exists to report, so the pod half may rely on nothing but the stdlib.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import BaseModel

from service_kit.governed.auth.write_model import model_document
from service_kit.governed.fga import MODEL_PAGE_SIZE


_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"

_STALE = {"id": "01J2STALE0000000000000000", "name": "lance-catalog", "created_at": "2026-07-15T09:00:00Z"}
_ESTATE = {"id": "01J2ESTATE000000000000000", "name": "lance-catalog", "created_at": "2026-07-15T09:00:03Z"}
_SCRATCH = {"id": "01J2SCRATCH00000000000000", "name": "fga-probe", "created_at": "2026-09-27T20:00:00Z"}


class _Estate(BaseModel):
    """What the stub OpenFGA serves: the store listing, and each store's models newest first."""

    stores: list[dict[str, Any]]
    histories: dict[str, list[dict[str, Any]]]


def _handler(estate: _Estate) -> type[BaseHTTPRequestHandler]:
    class _OpenFga(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            parts = url.path.strip("/").split("/")
            if parts == ["stores"]:
                self._send({"stores": estate.stores})
            elif len(parts) == 3 and parts[0] == "stores" and parts[2] == "authorization-models" and parts[1] in estate.histories:
                query = urllib.parse.parse_qs(url.query)
                if query.get("page_size") != [str(MODEL_PAGE_SIZE)]:
                    # Strict about the page size, so a verifier paging differently from the writers
                    # (whose page bound is counted in pages of MODEL_PAGE_SIZE) goes red here.
                    self.send_error(400)
                    return
                token = int(query.get("continuation_token", ["0"])[0])
                models = estate.histories[parts[1]]
                more = token + 1 < len(models)
                self._send({"authorization_models": models[token : token + 1], "continuation_token": str(token + 1) if more else ""})
            else:
                self.send_error(404)

        def _send(self, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the stdlib's own parameter name
            del format, args

    return _OpenFga


@pytest.fixture
def openfga() -> Iterator[Callable[[_Estate], str]]:
    servers: list[ThreadingHTTPServer] = []

    def serve(estate: _Estate) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(estate))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


def _stored(model_id: str, model: dict[str, Any]) -> dict[str, Any]:
    return {"id": model_id, **copy.deepcopy(model)}


def _drifted() -> tuple[dict[str, Any], str, str]:
    """This checkout's model with one relation dropped and one it never had: ``(model, dropped, extra)``."""
    model = model_document()
    kind = next(t for t in model["type_definitions"] if t.get("relations"))
    dropped = max(kind["relations"])
    del kind["relations"][dropped]
    kind["relations"]["retired_grant"] = {"this": {}}
    return model, f"{kind['type']}#{dropped}", f"{kind['type']}#retired_grant"


def _rule_changed() -> dict[str, Any]:
    """This checkout's model with one rule body replaced and every type and relation name kept."""
    model = model_document()
    body = next(body for t in model["type_definitions"] for body in (t.get("relations") or {}).values() if body != {"this": {}})
    body.clear()
    body["this"] = {}
    return model


# --------------------------------------------------------------------------- fga-store-check.sh


_FAKE_KUBECTL = """#!/usr/bin/env bash
# `get pods -o name` answers one catalog pod; `exec ... -- CMD` runs CMD here with the catalog's env.
case " $* " in
  *" get pods "*) echo "pod/rask-catalog-5d8f7c9b4-x2x7q"; exit 0 ;;
esac
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
export RASK_FGA_API_URL="$STUB_FGA_API_URL"
if [ "$1" = "python" ]; then shift; exec "$STUB_POD_PYTHON" -I -S "$@"; fi
exec "$@"
"""


def test_fga_store_check_passes_on_this_checkouts_model_held_below_the_newest(openfga: Callable[[_Estate], str], tmp_path: Path) -> None:
    drifted, _dropped, _extra = _drifted()
    api = openfga(
        _Estate(
            stores=[_STALE, _ESTATE],
            histories={
                _STALE["id"]: [_stored("model-STALE", drifted)],
                _ESTATE["id"]: [_stored("model-NEWER", drifted), _stored("model-CHECKOUT", model_document())],
            },
        )
    )
    fake = tmp_path / "bin" / "kubectl"
    fake.parent.mkdir()
    fake.write_text(_FAKE_KUBECTL, encoding="utf-8")
    fake.chmod(0o755)
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("RASK_FGA_STORE_ID", "NS")},
        "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
        "STUB_FGA_API_URL": api,
        "STUB_POD_PYTHON": sys.executable,
    }

    done = subprocess.run(["bash", str(_SCRIPTS / "fga-store-check.sh")], cwd=_ROOT, env=env, capture_output=True, text=True, check=False, timeout=180)  # noqa: S603, S607

    assert done.returncode == 0, f"a store holding this checkout's model below its newest failed the check:\n{done.stdout}\n{done.stderr}"
    for named in (_ESTATE["id"], "model-CHECKOUT", "model-NEWER"):
        assert named in done.stdout, f"the verdict does not name {named!r}:\n{done.stdout}"


@pytest.fixture
def store_check(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """`scripts/_fga_store_check.py`, imported the way `python scripts/_fga_store_check.py` resolves its sibling."""
    monkeypatch.syspath_prepend(str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_fga_store_check", _SCRIPTS / "_fga_store_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec, as Python registers a script: pydantic resolves the models' postponed
    # annotations through `sys.modules[cls.__module__]`.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _run(module: ModuleType, monkeypatch: pytest.MonkeyPatch, argv: list[str], stdin: str) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return module.main(argv)


def _pages(*models: dict[str, Any]) -> str:
    """Each model on its own page, one JSON page per line, as the pod half streams them."""
    return "".join(json.dumps({"authorization_models": [m], "continuation_token": "more" if i + 1 < len(models) else ""}) + "\n" for i, m in enumerate(models))


def test_the_store_check_uses_the_pinned_store_over_the_named_one(
    store_check: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = json.dumps({"pinned": "01J2PINNED000000000000000", "stores": [_STALE, _ESTATE]})

    assert _run(store_check, monkeypatch, ["plan"], listing) == 0
    assert capsys.readouterr().out.split()[0] == "01J2PINNED000000000000000"


def test_the_store_check_fails_when_no_lance_catalog_store_exists(
    store_check: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = json.dumps({"pinned": "", "stores": [_SCRATCH]})

    assert _run(store_check, monkeypatch, ["plan"], listing) == 1
    assert "lance-catalog" in capsys.readouterr().err


def test_the_store_check_passes_and_says_so_when_the_newest_model_is_this_checkouts(
    store_check: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    drifted, _dropped, _extra = _drifted()

    assert _run(store_check, monkeypatch, ["compare", _ESTATE["id"]], _pages(_stored("model-CHECKOUT", model_document()), _stored("model-OLDER", drifted))) == 0
    out = capsys.readouterr().out
    assert "model-CHECKOUT" in out
    assert "below" not in out, f"the store's newest model was reported as held below the newest:\n{out}"


def test_the_store_check_names_the_relation_diff_against_the_newest_when_the_model_is_absent(
    store_check: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    drifted, dropped, extra = _drifted()

    assert _run(store_check, monkeypatch, ["compare", _ESTATE["id"]], _pages(_stored("model-NEWER", drifted))) == 1
    out = capsys.readouterr().out
    assert dropped in out.split("NOT in the store:")[1].split("NOT in the repo:")[0], f"the missing relation is not listed as missing:\n{out}"
    assert extra in out.split("NOT in the repo:")[1], f"the extra relation is not listed as extra:\n{out}"


def test_the_store_check_does_not_read_an_empty_name_diff_as_a_match(
    store_check: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rule narrowed under unchanged names leaves the relation diff empty, and the model is still absent."""
    assert _run(store_check, monkeypatch, ["compare", _ESTATE["id"]], _pages(_stored("model-NEWER", _rule_changed()))) == 1
    assert "a rule differs under an unchanged name" in capsys.readouterr().out


def test_the_store_check_fails_on_a_store_that_holds_no_model(
    store_check: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(store_check, monkeypatch, ["compare", _ESTATE["id"]], json.dumps({"authorization_models": [], "continuation_token": ""}) + "\n") == 1
    assert "holds no authorization model" in capsys.readouterr().out
