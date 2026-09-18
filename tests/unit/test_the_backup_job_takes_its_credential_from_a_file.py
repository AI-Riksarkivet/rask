"""The control-root backup can be given its S3 credential as a FILE, never through the environment.

[[LH-110]]. The tool, the retention and the runbook all shipped; what was missing was the scheduled
lane, and the row offered three ways to ship the script without saying how the pod would authenticate.
`main` called `s3_client()` bare, so every one of those three ends at the same place: credentials on
the CronJob's environment. That is the one delivery path the estate forbids outright — "Never secret
through envs. Either from ESO, secret store dapr and STS for zero trust" — and a CronJob pod carries
no Dapr sidecar, so ESO writing a Secret this pod MOUNTS is the path that fits it.

`s3_client` already takes `access_key`/`secret_key`/`session_token` per call and its docstring says
they "override the env for THIS client", so nothing new was needed underneath — only a way to hand
them in that is not `os.environ`.

THE FILE IS READ, NOT THE ENVIRONMENT, and that is what these assert. A flag that silently fell back
to the env chain when the file was missing would satisfy a render test and reintroduce exactly the
delivery path this exists to avoid, so an unreadable credentials file is an ERROR rather than a
fallback.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("control_root_backup_creds", REPO_ROOT / "scripts" / "control_root_backup.py")
assert _SPEC and _SPEC.loader
crb = importlib.util.module_from_spec(_SPEC)
sys.modules["control_root_backup_creds"] = crb
_SPEC.loader.exec_module(crb)


@pytest.fixture
def credentials(tmp_path: Path) -> Path:
    path = tmp_path / "s3.json"
    path.write_text(json.dumps({"access_key": "AKIA-FROM-FILE", "secret_key": "SECRET-FROM-FILE", "session_token": "TOKEN-FROM-FILE"}))
    return path


def test_the_credentials_reach_the_client_from_the_file(credentials: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(crb, "s3_client", _spy)
    monkeypatch.setattr(crb, "do_backup", lambda *_a, **_k: {"backup": "s3://b/_backups/control/x"})

    crb.main(["backup", "--credentials-file", str(credentials)])

    assert seen.get("access_key") == "AKIA-FROM-FILE", f"the client was not given the file's key: {seen}"
    assert seen.get("secret_key") == "SECRET-FROM-FILE", f"the client was not given the file's secret: {seen}"
    assert seen.get("session_token") == "TOKEN-FROM-FILE", "a vended triple loses its session token, which fails open to the pod's own role"


def test_without_the_flag_nothing_is_forced_and_the_env_chain_still_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control. A developer running this by hand against a dev store passes no file, and the tool
    must not start demanding one — the flag exists for the scheduled pod, not for everybody."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(crb, "s3_client", lambda **kwargs: (seen.update(kwargs), object())[1])
    monkeypatch.setattr(crb, "do_backup", lambda *_a, **_k: {"backup": "s3://b/_backups/control/x"})

    crb.main(["backup"])

    # Named-but-None, not absent: the call always spells the keywords so it type-checks, and `None` is
    # precisely what makes `s3_client` fall through to its own env chain. A non-None here would mean
    # the tool invented a credential the caller never gave it.
    assert set(seen) == {"access_key", "secret_key", "session_token"}, f"the call shape changed: {seen}"
    assert not any(seen.values()), f"a bare run forced credentials the caller never supplied: {seen}"


def test_an_unreadable_credentials_file_is_an_ERROR_not_a_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Falling back to the environment would satisfy every render test and quietly restore the one
    delivery path this flag exists to remove."""
    monkeypatch.setattr(crb, "s3_client", lambda **_k: object())
    monkeypatch.setattr(crb, "do_backup", lambda *_a, **_k: {"backup": "s3://b/x"})

    with pytest.raises((SystemExit, OSError, ValueError)):
        crb.main(["backup", "--credentials-file", str(tmp_path / "absent.json")])
