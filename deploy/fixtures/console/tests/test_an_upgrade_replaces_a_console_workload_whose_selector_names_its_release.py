"""Each console workload selects its pods by this release, and is named by it, so one upgrade replaces it whenever its selector changes.

A Deployment's selector is immutable. The console's pods carry the standard `app.kubernetes.io/instance: <release>`, so
a Deployment named by anything but that release would keep its name across a change of release and its upgrade would be
refused ("field is immutable"), leaving a hand-run delete no reconciler makes. Named by the release, a changed selector
comes with a changed name, which Helm (or any reconciler that prunes) replaces in one apply. The Services select the same
labels, so traffic follows the pods across the replacement.
"""

from __future__ import annotations

from collections.abc import Callable


def test_each_console_workload_is_named_and_selected_by_the_release_that_renders_it(render_console: Callable[..., tuple[dict, ...]]) -> None:
    release = render_console()

    workloads = {doc["metadata"]["name"]: doc["spec"]["selector"]["matchLabels"] for doc in release if doc.get("kind") == "Deployment"}
    services = {doc["metadata"]["name"]: doc["spec"]["selector"] for doc in release if doc.get("kind") == "Service"}

    assert {name: labels["app.kubernetes.io/instance"] for name, labels in workloads.items()} == {"console-web": "console", "console-notifications": "console"}
    assert sorted(services.values(), key=str) == sorted(workloads.values(), key=str)
