"""`local-dir` must not be an arbitrary-file-read primitive aimed at the ingest pod.

`options.root` is caller-supplied and reaches `LocalDirSource`, which rglobs the tree and reads every
match into a governed Lance table. Unconfined — which is how this shipped — that is one request away
from publishing the pod's own secrets:

    {"kind": "local-dir", "project": "x", "dataset": "y",
     "options": {"root": "/proc/self", "pattern": "environ"}}

`/proc/self/environ` carries AWS_SECRET_ACCESS_KEY (verified present in the deployed pod's env), it is
extensionless so the payload validator waves it through, and the rows land where the explorer serves
them. The service-account token at /var/run/secrets/kubernetes.io/serviceaccount is the same shape.

Confinement is asserted at BOTH places the rule can be broken — the adapter that enumerates, and the
worker that reads. A unit key crosses the queue as a bare `file://` URI, so a check that lives only at
enumeration is bypassable by anything that can enqueue.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from ingest.adapters import LOCAL_ROOT_ENV, confine_to_local_root
from ingest.fetch import UriFetcher
from ingest.sources import SourceSpec, build_source


def test_an_UNSET_root_refuses_local_dir_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """No default, and "no default" must mean REFUSED rather than "read anything".

    A default of `/` — or of the process working directory — would be the same hole with an extra
    step. A source that cannot be pointed anywhere is a source nobody can abuse.
    """
    monkeypatch.delenv(LOCAL_ROOT_ENV, raising=False)

    with pytest.raises(ValueError, match=LOCAL_ROOT_ENV):
        build_source(SourceSpec(kind="local-dir", project="p", dataset="d", options={"root": "/etc"}))


@pytest.mark.parametrize(
    "attack",
    [
        "/proc/self",  # the process environment — the S3 credential
        "/var/run/secrets/kubernetes.io/serviceaccount",  # the service-account token
        "/etc",
        "/",
    ],
)
def test_a_path_OUTSIDE_the_root_is_refused(attack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The exfiltration targets, refused by name."""
    monkeypatch.setenv(LOCAL_ROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError, match="outside"):
        build_source(SourceSpec(kind="local-dir", project="p", dataset="d", options={"root": attack}))


def test_TRAVERSAL_out_of_the_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`..` is collapsed BEFORE the comparison.

    Comparing the raw string would accept `/allowed/../etc`, which resolves outside — the check has to
    run on the resolved path or it is decoration.
    """
    monkeypatch.setenv(LOCAL_ROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError, match="outside"):
        confine_to_local_root(str(tmp_path / ".." / ".." / "etc"))


def test_a_SYMLINK_pointing_out_of_the_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link inside the root must not become a door out of it.

    Resolving first is what makes this work: the symlink is followed, and the TARGET is what gets
    compared. Checking the link's own path would pass while the read happened elsewhere.
    """
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "escape").symlink_to("/etc")
    monkeypatch.setenv(LOCAL_ROOT_ENV, str(root))

    with pytest.raises(ValueError, match="outside"):
        confine_to_local_root(str(root / "escape"))


def test_the_root_ITSELF_and_paths_under_it_are_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not break the legitimate case it exists to protect."""
    monkeypatch.setenv(LOCAL_ROOT_ENV, str(tmp_path))
    nested = tmp_path / "vol" / "pages"
    nested.mkdir(parents=True)

    assert confine_to_local_root(str(tmp_path)) == tmp_path.resolve()
    assert confine_to_local_root(str(nested)) == nested.resolve()


def test_the_WORKER_refuses_an_out_of_root_key_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second check, and the one that actually stops an attack that got past enumeration.

    A unit key crosses the QUEUE as a bare `file://` URI. Whatever enumerated it is gone by the time a
    worker reads it, so confinement enforced only in the adapter is bypassable by anything able to
    enqueue — a redelivery, a hand-published message, a future producer.
    """
    import asyncio

    monkeypatch.setenv(LOCAL_ROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError, match="outside"):
        asyncio.run(UriFetcher().fetch("file:///proc/self/environ"))


def test_a_PERCENT_ENCODED_traversal_is_decoded_then_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`unquote` runs before confinement, so an encoded `../` is refused rather than passed through as
    an opaque string that only the filesystem later interprets."""
    import asyncio

    monkeypatch.setenv(LOCAL_ROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError, match="outside"):
        asyncio.run(UriFetcher().fetch(f"file://{tmp_path}/%2e%2e/%2e%2e/etc/hostname"))


def test_a_real_file_under_the_root_still_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard on, ordinary ingest unaffected."""
    import asyncio

    monkeypatch.setenv(LOCAL_ROOT_ENV, str(tmp_path))
    page = tmp_path / "page.tif"
    page.write_bytes(b"II*\x00PAGE")

    assert asyncio.run(UriFetcher().fetch(page.as_uri())) == b"II*\x00PAGE"


def test_the_pods_OWN_env_is_not_readable_even_when_the_root_is_permissive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The concrete attack, end to end, with a credential actually in the environment.

    Asserted with a real secret-shaped variable set, so the test fails loudly if the guard is ever
    relaxed to "warn" rather than "refuse".
    """
    import asyncio

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "a-real-looking-secret")
    monkeypatch.setenv(LOCAL_ROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError, match="outside"):
        asyncio.run(UriFetcher().fetch("file:///proc/self/environ"))

    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "a-real-looking-secret", "sanity: the secret WAS in the environment"
