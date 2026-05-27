"""control — operational logic library.

Consumed by `viewer`, the orchestrator app (later), and transitionally by the
wrapper scripts in `components/scripts/`. No entrypoints in this package —
callers own their CLI / HTTP / cron surface.
"""

from control.submission import SubmitResult as SubmitResult
from control.submission import build_entrypoint as build_entrypoint
from control.submission import chunk_name as chunk_name
from control.submission import submit_chunk as submit_chunk
from control.sync import SyncResult as SyncResult
from control.sync import reconcile_from_s3 as reconcile_from_s3
