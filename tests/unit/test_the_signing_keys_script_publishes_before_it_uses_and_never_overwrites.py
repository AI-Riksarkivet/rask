"""The operator's script creates the signing keys a store nothing seeds must hold: the public list first, and never over a pair that exists.

[[LH-064]]. The dev OpenBao mints its own pairs; a sealed or external store mints nothing, so the render refuses it until an
operator has created `signing-public-<identity>` and `signing-key-<identity>` for every identity the chart names and attests it.
`scripts/provision_signing_keys.sh` is the only way those keys get there, so it carries the seed's guarantees: a signer is Ready
only while its kid is listed, so the list is written before the seed that belongs to it; a pair that exists is not replaced, a
seed with no list or a list with no seed is refused rather than papered over, a rotation prepends and keeps one previous, and no
seed reaches an argument list or an output stream. Run here against the `bao` and `nk` stand-ins the seed's test uses.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.unit.chart_render import REPO
from tests.unit.openbao_standins import BAO, NK, install


SCRIPT = REPO / "scripts/provision_signing_keys.sh"
_A, _B = "service-a", "service-b"


def _put(stores: Path, name: str, **fields: str) -> None:
    path = stores / "local" / "secret" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{field}={value}\n" for field, value in fields.items()))


def _held(stores: Path, name: str, field: str) -> str | None:
    path = stores / "local" / "secret" / name
    lines = dict(line.split("=", 1) for line in path.read_text().splitlines()) if path.exists() else {}
    return lines.get(field)


@pytest.mark.parametrize(
    ("case", "args", "succeeds"),
    [
        pytest.param("created", (_A, _B), True, id="an-absent-pair-is-created"),
        pytest.param("exists", (_A,), True, id="a-pair-that-exists-is-left-alone"),
        pytest.param("rotated", ("--rotate", _A), True, id="a-rotation-prepends-and-keeps-one-previous"),
        pytest.param("key-without-list", (_A,), False, id="a-seed-without-its-list-is-refused"),
        pytest.param("list-without-key", (_A,), False, id="a-list-without-its-seed-needs-an-explicit-rotation"),
    ],
)
def test_the_script_writes_the_list_before_the_seed_and_never_over_a_pair(
    tmp_path: Path, event_signer: Any, case: str, args: tuple[str, ...], succeeds: bool
) -> None:  # noqa: FBT001
    stores = tmp_path / "stores"
    (stores / "local").mkdir(parents=True)
    pool = [event_signer(f"pair-{i}") for i in range(4)]
    fresh, other, old, older = pool
    expected: dict[str, tuple[str | None, str | None]] = {_A: (None, None), _B: (None, None)}
    if case == "created":
        expected = {_A: (fresh.seed, fresh.public), _B: (pool[1].seed, pool[1].public)}
    elif case == "exists":
        _put(stores, f"signing-key-{_A}", seed=other.seed)
        _put(stores, f"signing-public-{_A}", keys=other.public)
        expected[_A] = (other.seed, other.public)
    elif case == "rotated":
        _put(stores, f"signing-key-{_A}", seed=other.seed)
        _put(stores, f"signing-public-{_A}", keys=f"{old.public},{older.public}")
        expected[_A] = (fresh.seed, f"{fresh.public},{old.public}")
    elif case == "key-without-list":
        _put(stores, f"signing-key-{_A}", seed=other.seed)
        expected[_A] = (other.seed, None)
    elif case == "list-without-key":
        _put(stores, f"signing-public-{_A}", keys=old.public)
        expected[_A] = (None, old.public)
    (tmp_path / "pairs").write_text("".join(f"{pair.seed}\n{pair.public}\n" for pair in pool))
    install(tmp_path, bao=BAO, nk=NK)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "BAO_ADDR": "http://127.0.0.1:8200",
        "SERVICE_ADDR": "",
        "STORES": str(stores),
        "NK_PAIRS": str(tmp_path / "pairs"),
        "NK_STATE": str(tmp_path / "nk-calls"),
        "NK_FAIL": "",
        "NK_GARBAGE": "",
    }

    ran = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603, S607

    calls_text = (stores / "calls").read_text() if (stores / "calls").exists() else ""
    calls = calls_text.splitlines()
    assert (ran.returncode == 0) is succeeds, f"exit {ran.returncode}: {ran.stderr[-1500:]}"
    leaked = [pair.seed for pair in pool if pair.seed in ran.stdout + ran.stderr + calls_text]
    assert not leaked, "a private seed reached an output stream or a bao argument list"
    for identity, pair in expected.items():
        assert (_held(stores, f"signing-key-{identity}", "seed"), _held(stores, f"signing-public-{identity}", "keys")) == pair, f"{case}: {identity}"
        public_put = next((i for i, call in enumerate(calls) if f" kv put secret/signing-public-{identity} " in call), None)
        key_put = next((i for i, call in enumerate(calls) if f" kv put secret/signing-key-{identity} " in call), None)
        if case in {"created", "rotated"} and identity in args:
            assert public_put is not None and key_put is not None and public_put < key_put, f"{identity}: the list must be written before the seed it publishes"
        else:
            assert public_put is None and key_put is None, f"{case}: {identity} was written"
