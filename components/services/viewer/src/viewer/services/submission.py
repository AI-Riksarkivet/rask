"""Async subprocess wrappers for the two operator scripts the API exposes.

Kept thin — when the heavy refactor lands, the script logic moves into a
control-plane brick and these become direct service calls. Until then,
`asyncio.create_subprocess_exec` keeps the event loop free while the
underlying scripts run.
"""

import asyncio
import logging
import sys
from pathlib import Path

from viewer.core.exceptions import ServiceUnavailableError, UpstreamTimeoutError, UpstreamUnavailableError


log = logging.getLogger(__name__)


async def _run(argv: list[str], cwd: Path, timeout: float) -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise UpstreamTimeoutError(f"subprocess timed out after {timeout}s") from exc
    if proc.returncode != 0:
        msg = (err.decode(errors="replace") or out.decode(errors="replace"))[-500:]
        raise UpstreamUnavailableError(f"subprocess failed (rc={proc.returncode}): {msg}")
    return out.decode(errors="replace"), err.decode(errors="replace")


async def run_sync_script(scripts_dir: Path, repo_root: Path, timeout: float) -> None:
    script = scripts_dir / "sync_from_s3.py"
    if not script.exists():
        raise ServiceUnavailableError(f"missing {script}")
    await _run([sys.executable, str(script)], cwd=repo_root, timeout=timeout)


async def submit_chunk(scripts_dir: Path, repo_root: Path, chunk_id: int, timeout: float) -> str:
    script = scripts_dir / "submit_chunks.py"
    if not script.exists():
        raise ServiceUnavailableError(f"missing {script}")
    stdout, _ = await _run(
        [sys.executable, str(script), "--mode", "local", "--chunk", str(chunk_id)],
        cwd=repo_root,
        timeout=timeout,
    )
    return stdout.strip()
