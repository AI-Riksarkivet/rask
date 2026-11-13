"""Evidence no door can write, planted from inside the catalog pod under the catalog's own store session.

A guard's refusal to judge evidence it cannot read shows only on evidence that is there and cannot be read: a committed
manifest that does not decode, a registry record that is not JSON. No credential a test holds writes either (a write vend
Denies every final manifest, `catalog.core.vending.build_session_policy`; the registry is the catalog's alone), so the
fault is written by a process in the catalog's own container, which exchanges the pod's projected ServiceAccount token
for a session as the catalog's lifespan does (`lakehouse.store_session.start_store_session`). No root key is used.

`planted` writes junk at one key, yields while the test asks the door, and in the same process puts back what was there
(or removes the key it made) and reads it back equal: the window during which the fault stands is the test's request.
Only a scratch object of the calling test is ever named.
"""

from __future__ import annotations

import select
import subprocess
from collections.abc import Generator
from contextlib import contextmanager

from chaos import CATALOG, KUBE_NAMESPACE


#: How long the process in the pod may take to plant or to restore: a `kubectl exec` and an STS exchange, each in seconds.
ANSWER_SECONDS = 60.0

#: Run in the catalog's container: ``argv`` is the root (an `s3://` URI, or ``control`` for the catalog's registry root),
#: the key under it, and ``replace`` (the key must hold an object) or ``create`` (it must hold none). It prints
#: ``planted`` once the junk stands, waits for a line on stdin, restores, and prints ``restored`` once a re-read answers
#: what was there before (or nothing, for a key it made); ``absent`` or ``present`` when the key was not as asked.
_IN_POD = """
import asyncio, sys
from catalog.core.config import get_settings
from lakehouse.objectfs import fs_and_base
from lakehouse.store_session import start_store_session
from lakehouse.supervision import Supervisor

JUNK = b"planted by tests/e2e-py/plant.py: not a manifest, not a record"

async def main(root: str, key: str, mode: str) -> None:
    settings = get_settings()
    loops = Supervisor()
    session = await start_store_session(settings.web_identity, session_name="e2e-plant", duration_seconds=settings.s3_session_seconds, loops=loops)
    try:
        fs, base = fs_and_base(settings.registry_root if root == "control" else root, settings.storage_options())
        path = f"{base}/{key}"
        try:
            with fs.open_input_stream(path) as stream:
                before = stream.read()
        except FileNotFoundError:
            before = None
        if (before is None) != (mode == "create"):
            print("absent" if before is None else "present", flush=True)
            return
        with fs.open_output_stream(path) as out:
            out.write(JUNK)
        print("planted", flush=True)
        sys.stdin.readline()
        if before is None:
            fs.delete_file(path)
            gone = fs.get_file_info(path).type.name == "NotFound"
            print("restored" if gone else "left", flush=True)
        else:
            with fs.open_output_stream(path) as out:
                out.write(before)
            with fs.open_input_stream(path) as stream:
                print("restored" if stream.read() == before else "left", flush=True)
    finally:
        if session is not None:
            await session.stop()
        loops.wind_down()

asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
"""


def _said(process: subprocess.Popen[str], waiting_for: str) -> str:
    """The next line the process in the pod printed, within `ANSWER_SECONDS`; a process that says nothing is killed."""
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], ANSWER_SECONDS)
    if not ready:
        process.kill()
        raise AssertionError(f"the process in the catalog pod said nothing within {ANSWER_SECONDS:.0f} s while {waiting_for}")
    return process.stdout.readline().strip()


@contextmanager
def planted(root: str, key: str, *, replacing: bool) -> Generator[None]:
    """Junk at ``key`` under ``root`` (an `s3://` URI, or ``"control"`` for the catalog's registry root) while the block
    runs, over the object there when ``replacing``, else at a key that holds none; what was there before is back, read
    back equal, when it ends, however the block ended."""
    mode = "replace" if replacing else "create"
    command = [
        "kubectl",
        *(("--namespace", KUBE_NAMESPACE) if KUBE_NAMESPACE else ()),
        "exec",
        "-i",
        f"deploy/{CATALOG}",
        "-c",
        "catalog",
        "--",
        "/opt/venv/bin/python",
        "-c",
        _IN_POD,
        root,
        key,
        mode,
    ]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        said = _said(process, f"planting {root}/{key}")
        if said != "planted":
            process.kill()
            raise AssertionError(f"the fault at {root}/{key} was not planted: {said!r} {process.stderr.read()[-800:]}")
        try:
            yield
        finally:
            process.stdin.write("restore\n")
            process.stdin.flush()
            restored = _said(process, f"restoring {root}/{key}")
            process.wait(timeout=ANSWER_SECONDS)
            assert restored == "restored", f"{root}/{key} was not put back as it was: {restored!r} {process.stderr.read()[-800:]}"
