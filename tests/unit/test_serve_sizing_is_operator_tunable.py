"""Every Serve deployment's sizing answers to the SAME two env names, or an operator cannot size it.

open_ray-kernel.md move 3, the last cross-runner duplication the review left unpinned: the sizing
knobs `RASK_SERVE_REPLICAS` / `RASK_SERVE_GPU_FRAC` were read by three deployments and hardcoded by
two others (`topics` pinned `num_replicas=1, num_gpus=0` in the decorator; the htr deploy script
pinned `4`/`1`). A deployment that hardcodes its sizing cannot be resized on a smaller host, packed
beside another workload, or scaled up — without editing a sealed runner's source.

THE INVARIANT IS THE KNOB NAMES, NOT THE VALUES. CLAUDE.md's rule stands: replica counts and GPU
fractions are the RUNNER's business — htr defaults 2/0.49 (two replicas packed per card), voiceprint
1/0 (CPU) — and this gate does not touch that. What it pins is that every deployment exposes the
SAME two env names, so one operator gesture sizes any workload. The same shape as the memory knob
(`RASK_SERVE_MEMORY_GB`) the OOM-cascade fix added.

WHY AN AST GATE AND NOT SHARED CODE — the dissolution's settled answer: six of nine runner
environments cannot install any platform package (`requires-python >=3.10,<3.13` vs the platform's
`>=3.13`), so a shared constants module is uninstallable where it is needed most. Reading the AST
needs no import, which is how one root-suite test holds all nine sealed runners plus their deploy
scripts to the rule.

TWO HALVES, both load-bearing:
- a decorator sizing value that is a bare LITERAL is flagged (hardcoded — the defect);
- a sizing value that is a NAME must live in a file that actually reads the standard env var, or
  the name is just a literal wearing an indirection.
"""

from __future__ import annotations

import ast
import pathlib

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]

REPLICAS_ENV = "RASK_SERVE_REPLICAS"
GPU_ENV = "RASK_SERVE_GPU_FRAC"


def _runner_sources() -> list[pathlib.Path]:
    return [p for p in sorted((REPO / "runners").rglob("*.py")) if ".venv" not in p.parts and "site-packages" not in p.parts and "tests" not in p.parts]


def _serve_deployment_decorators(tree: ast.Module) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "deployment":
                calls.append(dec)
    return calls


def _sizing_offences(path: pathlib.Path) -> list[str]:
    text = path.read_text()
    offences = []
    for call in _serve_deployment_decorators(ast.parse(text)):
        for kw in call.keywords:
            if kw.arg == "num_replicas":
                if isinstance(kw.value, ast.Constant):
                    offences.append(f"{path.relative_to(REPO)}:{kw.value.lineno} num_replicas is the literal {kw.value.value!r}")
                elif REPLICAS_ENV not in text:
                    offences.append(f"{path.relative_to(REPO)}:{kw.value.lineno} num_replicas is a name, but nothing in the file reads {REPLICAS_ENV}")
            if kw.arg == "ray_actor_options":
                if isinstance(kw.value, ast.Dict):
                    for key, value in zip(kw.value.keys, kw.value.values, strict=True):
                        if isinstance(key, ast.Constant) and key.value == "num_gpus":
                            if isinstance(value, ast.Constant):
                                offences.append(f"{path.relative_to(REPO)}:{value.lineno} num_gpus is the literal {value.value!r}")
                            elif GPU_ENV not in text:
                                offences.append(f"{path.relative_to(REPO)}:{value.lineno} num_gpus is a name, but nothing in the file reads {GPU_ENV}")
                elif isinstance(kw.value, ast.Name) and GPU_ENV not in text:
                    offences.append(f"{path.relative_to(REPO)}:{kw.value.lineno} ray_actor_options is {kw.value.id}, but nothing in the file reads {GPU_ENV}")
    return offences


def test_the_gate_sees_deployments_at_all() -> None:
    seen = [p for p in _runner_sources() if _serve_deployment_decorators(ast.parse(p.read_text()))]
    assert len(seen) >= 4, f"the walk found almost no Serve deployments ({seen}) — it is checking nothing"


def test_every_deployment_sizing_is_env_tunable() -> None:
    offences = [o for p in _runner_sources() for o in _sizing_offences(p)]
    assert not offences, (
        "these Serve deployments hardcode their sizing, so an operator cannot resize or co-pack them "
        f"without editing a sealed runner's source — read {REPLICAS_ENV}/{GPU_ENV} with the workload's "
        "own defaults instead:\n  " + "\n  ".join(offences)
    )


@pytest.mark.parametrize("path", ["runners/htr/src/runner/transcribe_service.py", "runners/voiceprint/voiceprint_service.py"])
def test_the_deployments_that_already_do_this_pass(path: str) -> None:
    assert not _sizing_offences(REPO / path), f"{path} reads the standard knobs and was flagged — the rule has gone wrong"
