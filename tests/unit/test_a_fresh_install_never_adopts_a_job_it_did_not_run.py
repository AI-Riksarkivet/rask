"""A fresh install deletes any Job a previous lifetime of the release left behind, before creating its own.

The bootstrap Jobs are named by revision (`<release>-openbao-seed-r<N>` and friends), and a fresh
install starts again at revision 1. `helm uninstall` deletes only the Jobs its LAST revision's manifest
names, so when that revision failed before applying (an upgrade stopped by a pre-upgrade hook), the
Jobs of the revision before it outlive the uninstall. The next install finds Jobs with its exact names,
adopts them (`--take-ownership`), and never runs its own.

Measured 2026-09-23: an upgrade to r2 failed in its pre-upgrade hook, `make k3s-purge` uninstalled r2's
manifest (Jobs named `-r2`, never created), and seven `-r1` Jobs created at 19:00:24Z survived. The
fresh install at 19:46 adopted the completed `rask-openbao-seed-r1`, so the new OpenBao was never
seeded: the SecretStore could not log in, the three ExternalSecrets sat in SecretSyncedError, and the
workloads waiting on `rask-infra-credentials` never started.

So the release's one pre-install hook, which runs before any object of the new release exists, deletes
every Job carrying this release's instance label, and excludes only itself.
"""

from __future__ import annotations

import itertools

from tests.unit import chart_render


REPO = chart_render.REPO
NAMESPACE = "probe-ns"


def _docs() -> tuple[dict, ...]:
    return chart_render.render(*chart_render.DEFAULT_ARGS, "-f", str(REPO / "chart" / "values-local.yaml"), "--namespace", NAMESPACE)


def _annotations(doc: dict) -> dict:
    return doc.get("metadata", {}).get("annotations") or {}


def _hooks(doc: dict) -> str:
    return _annotations(doc).get("helm.sh/hook", "")


def _weight(doc: dict) -> int:
    return int(_annotations(doc).get("helm.sh/hook-weight", "0"))


def _selects(selector: str, labels: dict[str, str]) -> bool:
    """A kubectl equality-based label selector: `k=v`, `k==v` and `k!=v` terms, all of which must hold."""
    for term in selector.split(","):
        if "!=" in term:
            key, value = term.split("!=", 1)
            if labels.get(key) == value:
                return False
        else:
            key, value = term.replace("==", "=").split("=", 1)
            if labels.get(key) != value:
                return False
    return True


def _sweeps(docs: tuple[dict, ...]) -> list[tuple[dict, list[str]]]:
    """(job, args) of every pre-install container that deletes Jobs."""
    found = []
    for job in (d for d in docs if d.get("kind") == "Job" and "pre-install" in _hooks(d)):
        spec = job["spec"]["template"]["spec"]
        for container in [*(spec.get("initContainers") or []), *spec["containers"]]:
            args = [str(a) for a in container.get("args") or []]
            if args[:1] == ["delete"] and any(a.split("/")[0].split(".")[0] in ("jobs", "job") for a in args):
                found.append((job, args))
    return found


def _selector(args: list[str]) -> str:
    pairs = list(itertools.pairwise(args))
    [selector] = [v for k, v in pairs if k in ("-l", "--selector")] or [""]
    return selector


def test_every_job_the_release_creates_is_swept_before_a_fresh_install() -> None:
    docs = _docs()
    sweeps = _sweeps(docs)
    assert sweeps, "no pre-install hook deletes leftover Jobs, so a fresh install adopts any Job with its name"

    jobs = [d for d in docs if d.get("kind") == "Job" and not _hooks(d)]
    assert jobs, "the release renders no Job — this gate is looking at the wrong render"
    for _, args in sweeps:
        assert ("-n", NAMESPACE) in list(itertools.pairwise(args)), f"the sweep does not delete in the release namespace: {args}"
    missed = [j["metadata"]["name"] for j in jobs if not any(_selects(_selector(args), j["metadata"].get("labels") or {}) for _, args in sweeps)]
    assert not missed, f"a leftover of these Jobs would be adopted by the next fresh install: {missed}"


def test_the_sweep_runs_first_and_spares_itself() -> None:
    """It deletes by label, so it must neither select itself nor run after another pre-install Job of
    the SAME install, which it would then delete mid-run."""
    docs = _docs()
    sweeps = _sweeps(docs)
    assert sweeps, "no pre-install hook deletes leftover Jobs"

    for job, args in sweeps:
        assert not _selects(_selector(args), job["metadata"].get("labels") or {}), f"the sweep's selector selects the sweep itself: {_selector(args)!r}"
        assert "--wait=false" not in args, "the sweep must finish deleting before the install creates Jobs of the same names"
        later = [d["metadata"]["name"] for d in docs if d.get("kind") == "Job" and "pre-install" in _hooks(d) and d is not job and _weight(d) <= _weight(job)]
        assert not later, f"pre-install Jobs that run no later than the sweep, which it could delete: {later}"


def test_the_sweep_may_delete_jobs() -> None:
    docs = _docs()
    sweeps = _sweeps(docs)
    assert sweeps, "no pre-install hook deletes leftover Jobs"

    for job, _ in sweeps:
        account = job["spec"]["template"]["spec"]["serviceAccountName"]

        def earlier(doc: dict, job: dict = job) -> bool:
            return "pre-install" in _hooks(doc) and _weight(doc) < _weight(job)

        assert any(d.get("kind") == "ServiceAccount" and d["metadata"]["name"] == account and earlier(d) for d in docs), (
            f"the sweep runs as {account!r}, which no earlier pre-install hook creates"
        )
        roles = {
            (d["roleRef"]["kind"], d["roleRef"]["name"])
            for d in docs
            if d.get("kind") in ("RoleBinding", "ClusterRoleBinding")
            and earlier(d)
            and any(s.get("name") == account and s.get("namespace") == NAMESPACE for s in d.get("subjects") or [])
        }
        rules = [r for d in docs if (d.get("kind"), d["metadata"].get("name")) in roles and earlier(d) for r in d.get("rules") or []]
        verbs = {v for r in rules if "batch" in (r.get("apiGroups") or []) and "jobs" in (r.get("resources") or []) for v in r.get("verbs") or []}
        assert {"delete", "list", "get", "watch"} <= verbs, f"the sweep may not select, delete and wait on jobs; granted {sorted(verbs)}"
