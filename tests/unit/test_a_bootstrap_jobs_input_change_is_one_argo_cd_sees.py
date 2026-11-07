"""A changed input of a bootstrap Job changes an object Argo CD compares, so the next sync runs the Job again ([[XC-164]]).

Argo CD leaves its hooks out of the state it compares (gitops-engine `pkg/sync/reconcile.go` `splitHooks`, read at v3.5.7),
and every bootstrap Job is a hook there (`lance.reconciledJob`). A value that changed only a Job, the streams' max age or a
store policy, would leave the Application Synced, and no auto-sync would run the Job that applies it.
`<release>-bootstrap-inputs` is a plain ConfigMap holding, per bootstrap template, a digest of its Jobs' specs.
"""

from __future__ import annotations

from tests.unit.chart_render import DEFAULT_ARGS, by_source


def _inputs(*extra: str) -> dict[str, str]:
    [inputs] = [doc for _, doc in by_source(*DEFAULT_ARGS, *extra) if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "rask-bootstrap-inputs"]
    return inputs["data"]


def test_a_changed_bootstrap_input_changes_the_digest_of_exactly_its_template() -> None:
    before = _inputs()

    after = _inputs("--set", "nats.streamMaxAgeSeconds=259200")

    hooks = {
        source.removeprefix("templates/") for source, doc in by_source(*DEFAULT_ARGS) if "argocd.argoproj.io/hook" in (doc["metadata"].get("annotations") or {})
    }
    assert {template for template in after if after[template] != before[template]} == {"nats-stream-job.yaml"}
    assert hooks <= after.keys(), f"these templates render an Argo CD hook the digest does not cover: {sorted(hooks - after.keys())}"
