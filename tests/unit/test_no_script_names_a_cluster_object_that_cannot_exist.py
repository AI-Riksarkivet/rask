"""No executable surface may name a `rask-*` cluster object the chart cannot produce.

Three lived in the tree: `scripts/verify_lance_storage.py` told a reader to fetch credentials from
`secret/rask-rustfs` — on the line below one that correctly says `svc/rask-minio` — and two e2e
suites described the vended endpoint as `http://rask-rustfs-io:9000`. None of the three exists, in
the chart or on the running estate. A runbook that does not run is worse than no runbook: it costs a
reader the time to try it and then the time to distrust everything around it.

THE SWEEP IS ONE-SIDED AND SO IS THE GATE. A name the chart can produce is fine whether or not this
particular estate has it enabled; a name the chart can NEVER produce is dead everywhere. So the known
set is the UNION of two answers, because neither is sufficient alone:

* the DEFAULT RENDER catches names that come from a subchart — `rask-minio` is rendered by one and
  appears in no template of ours;
* the TEMPLATE SCAN catches names behind a values toggle — `rask-ray-auth-token` renders only with
  token auth on, and a render-only check called three honest references dead. That was three of my
  first six findings, and the correction is why the union exists.

SCOPED TO EXECUTABLE SURFACES: scripts, applied manifests and the e2e suites. `docs/` is excluded
because prose legitimately records what a name USED to be, and a gate that could not tell the two
apart would push people to delete history instead of keeping it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_SURFACES = ("scripts/*.sh", "scripts/*.py", "deploy/*.yaml", "tests/e2e-py/*.py")
#: A cluster DNS host in a URL, and a `kubectl get secret <name>` / `secret/<name>` reference.
_HOST = re.compile(r"https?://(rask-[a-z0-9-]+)[:/]")
_SECRET = re.compile(r"(?:secret|secrets)[/ ]+(rask-[a-z0-9-]+)")
_FULLNAME = re.compile(r'\{\{-?\s*include\s+"(?:rask|lance)\.fullname"\s+\.\s*-?\}\}')


def _rendered_names() -> set[str]:
    """Every `metadata.name` a default render produces, plus the `-hl` headless siblings."""
    done = subprocess.run(
        [
            "helm",
            "template",
            "rask",
            "chart/",
            "--set",
            "image.localImages=true",
            "--set",
            "frontend.oidc.sessionSecret=0123456789abcdef0123456789abcdef",
            "--set",
            "frontend.oidc.publicIssuer=http://dex.local:5556",
            "--set",
            "frontend.oidc.publicOrigin=http://rask.local",
            "--set",
            "frontend.oidc.clientSecret=abcdef0123456789abcdef0123456789",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip(f"helm could not render the chart here: {done.stderr.strip()[:200]}")
    names: set[str] = set()
    for doc in yaml.safe_load_all(done.stdout):
        if isinstance(doc, dict) and isinstance(doc.get("metadata"), dict) and doc["metadata"].get("name"):
            names.add(str(doc["metadata"]["name"]))
            names.add(f"{doc['metadata']['name']}-hl")
    return names


def _templated_names() -> set[str]:
    """Every `name:` a template can produce, with the release helpers resolved textually.

    Textual on purpose: rendering with every toggle on is a combination nobody runs and would be its
    own source of wrong answers. Over-approximating is the safe direction here — this gate only ever
    claims that a name can NEVER be produced.
    """
    names: set[str] = set()
    for path in (_ROOT / "chart" / "templates").rglob("*.yaml"):
        for line in path.read_text(errors="ignore").splitlines():
            found = re.search(r"^\s*name:\s*(\S.*)$", line)
            if not found:
                continue
            value = _FULLNAME.sub("rask", found.group(1).strip().strip("\"'"))
            value = value.replace("{{ .Release.Name }}", "rask").replace("{{ $root.Release.Name }}", "rask")
            if re.fullmatch(r"rask-[a-z0-9-]+", value):
                names.add(value)
    return names


@pytest.fixture(scope="module")
def known() -> set[str]:
    return _rendered_names() | _templated_names()


def test_both_halves_of_the_known_set_contribute(known: set[str]) -> None:
    """Anti-vacuity, and it pins the reason there are two halves at all.

    `rask-minio` comes only from the render (a subchart's object) and `rask-ray-auth-token` only from
    the templates (behind a toggle). Either half alone produces false verdicts, which is exactly what
    happened on the first pass.
    """
    assert len(known) > 50, f"only {len(known)} names known — one of the two halves is not contributing"
    assert "rask-minio" in _rendered_names(), "the render half stopped seeing the object store"
    assert "rask-ray-auth-token" in _templated_names(), "the template half stopped seeing toggle-gated names"


def test_no_executable_surface_names_an_impossible_object(known: set[str]) -> None:
    dead: list[str] = []
    scanned = 0
    for pattern in _SURFACES:
        for path in sorted(_ROOT.glob(pattern)):
            scanned += 1
            text = path.read_text(errors="ignore")
            for name in sorted(set(_HOST.findall(text)) | set(_SECRET.findall(text))):
                if name not in known:
                    dead.append(f"{path.relative_to(_ROOT)}: {name}")

    assert scanned > 20, f"only {scanned} files scanned — the globs are not reaching the tree"
    assert not dead, "these executable surfaces name cluster objects the chart cannot produce, so following them fails against any estate:\n  " + "\n  ".join(
        dead
    )
