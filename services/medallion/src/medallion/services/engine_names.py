"""The one engine name the lakehouse itself defines: its own in-process reference engine.

Every other engine is a key of the deployment's `MEDALLION_ENGINES`, reached by service invocation
(`service_kit.lakehouse.engine_door`), so its name is configuration and appears in no module (CP-056). This module
imports nothing, so the registry, the chooser and the task registrar can all take the name from here without a cycle.
"""

from __future__ import annotations

from typing import Final


#: A stage run in the calling process. The adapter is `inprocess_executor.InProcessExecutor`.
IN_PROCESS_ENGINE: Final = "inprocess"
