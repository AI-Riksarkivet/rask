"""Domain errors for the sealed ``topics`` runner.

Vendored from ``ratch.errors`` at the ratch dissolution (2026-08-28 —
``open_ray-kernel.md``, move 10). It is a COPY rather than an import, and that
is the seal working rather than drift: this runner is SEALED — its own
``pyproject.toml`` pins ``requires-python = ">=3.10,<3.13"`` (Toponymy needs
``transformers < 5``), while every platform package (``ratch``, ``service-kit``,
``ray-kit``) is ``>=3.13``. No platform package can be imported here at all, so
an exception class the runner owns is the only honest way to have one.

Runner code raises :class:`TopicsError` — never ``SystemExit``: exiting the
process is the CALLER's decision, and a raised exit kills embedding callers and
tests. Whatever drives the runner (a CLI, a Ray job, a Serve replica) catches it
and maps it to a clean message plus its own exit / failure policy.
"""

from __future__ import annotations


class TopicsError(Exception):
    """A user-actionable failure — a bad input path, a missing column, or a
    modelling error such as driving corpus-global topics as a per-batch stage.

    The message is shown verbatim to whoever ran the runner — keep it specific
    enough to act on (what was attempted, and which form does work).
    """
