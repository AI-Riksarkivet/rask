"""The engine names, defined ONCE.

`RAY_ENGINE` was declared in three modules and `IN_PROCESS_ENGINE` in two. They agreed, and nothing
made them agree: a rename in one is a stage that chooses an engine no adapter answers to, and the
failure surfaces as an `UnrunnableTaskError` naming an engine that looks correct in whichever file
the reader happens to open.

A NAME MODULE RATHER THAN A HOME IN ONE OF THEM, because every candidate creates a cycle. The
registry imports the adapters, the adapters need the name, and `engine_choice` needs it to answer
which engine a stage runs on. This module imports nothing, so all four can take the name from here.

It deliberately holds NO set of hosted engines. `engine_choice.HOSTED_ENGINES` declares what this
deployment may choose and `engine_registry.hosted_engines()` reports what can actually be resolved;
those are different questions, and collapsing them here would make the test that compares them
trivially true.
"""

from __future__ import annotations

from typing import Final


#: A stage submitted to Ray. The adapter is `rayjob_executor.RayJobExecutor`.
RAY_ENGINE: Final = "ray"

#: A stage run in the calling process. The adapter is `inprocess_executor.InProcessExecutor`.
IN_PROCESS_ENGINE: Final = "inprocess"
