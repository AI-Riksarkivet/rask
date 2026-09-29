"""The authorization model is written before an upgrade's new pods start ([[LH-201]]).

A service resolves the model its image carries at boot. Written after the upgrade, the model a new image
carries does not exist when that image's pods start, so they wait and then fail closed. `pre-upgrade`
runs the hook against the release's new image before any of its resources change; `post-install` stays
for a first install, where OpenFGA itself is one of the resources and there is nothing to write into yet.

Read off the source: the rule is about how the template is written, and the annotation renders the
same under every value set.
"""

from __future__ import annotations

import re
from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[2] / "chart" / "templates" / "openfga-model.yaml"


def _hooks() -> set[str]:
    found = re.search(r'"helm\.sh/hook":\s*([^\n]+)', TEMPLATE.read_text(encoding="utf-8"))
    assert found, "the openfga-model Job is no longer a hook"
    return {hook.strip() for hook in found.group(1).strip().strip('"').split(",")}


def test_the_model_hook_runs_before_an_upgrade_and_after_a_first_install() -> None:
    assert _hooks() == {"post-install", "pre-upgrade"}
