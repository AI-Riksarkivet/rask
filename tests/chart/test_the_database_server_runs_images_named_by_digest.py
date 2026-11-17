"""On the AGE-on-CNPG path, the database server and the extension loaded into it are images named by digest ([[XC-138]]).

A tag is mutable, so a Cluster naming one runs whatever the registry holds at its next pull, and the AGE extension is
code loaded into the server holding OpenFGA's datastore. The Cluster's own image is the chart's, pinned to the digest
`.docker/cnpg-age-ext.dockerfile` builds the extension against; the extension image is the operator's value, and a
render naming it by tag alone is refused.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from tests.chart.chart_render import DEFAULT_ARGS, render, render_text


_DIGEST = "sha256:" + "ab" * 32
_BY_DIGEST = re.compile(r".+@sha256:[0-9a-f]{64}")
#: The AGE-on-CNPG path on a cluster that serves the CloudNativePG API.
_CNPG = (*DEFAULT_ARGS, "--api-versions", "postgresql.cnpg.io/v1/Cluster", "--set", "age.enabled=false", "--set", "age.cnpgCluster.enabled=true")


def test_the_cluster_and_its_extension_are_named_by_digest() -> None:
    [cluster] = [doc for doc in render(*_CNPG, "--set-string", f"age.cnpgCluster.extensionImage=reg.example/age@{_DIGEST}") if doc.get("kind") == "Cluster"]

    assert _BY_DIGEST.fullmatch(cluster["spec"]["imageName"]), cluster["spec"]["imageName"]
    assert [extension["image"]["reference"] for extension in cluster["spec"]["postgresql"]["extensions"]] == [f"reg.example/age@{_DIGEST}"]


def test_an_extension_image_named_by_tag_alone_is_refused() -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*_CNPG, "--set-string", "age.cnpgCluster.extensionImage=reg.example/age:1.7.0-18")

    assert 'age.cnpgCluster.extensionImage is "reg.example/age:1.7.0-18", which names no digest' in refused.value.stderr
