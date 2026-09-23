"""A fresh host's `make k3s-up` asks the node only for images `make k3s-build` made, at the tag it stamped.

The side-loaded path is three targets that must agree on two facts — WHICH images and WHAT TAG — and
nothing made them agree. Measured 2026-09-23 on a fresh arm64 host after `make k3s-build` +
`make k3s-import`: the node held fifteen first-party images, all `:dev`, and the first install's render
asked for `flows:dev` (never built), `web-<zone>:7ea481c` for all seven zones (the chart's zone
default), and `localhost:5000/ray-cluster:main-2cfe68f4` for the Ray head (a registry this host does
not run). Every one of those pods would have sat in ImagePullBackOff until `--wait` gave up.

The render is the one `make k3s-up` would really perform on a cluster with no release: its upgrade
shell is executed with helm replaced by an argument printer (`tests/unit/k3s_recipe.py`), so the flags
under test are the recipe's own, not a copy of them. The build and import targets are executed the
same way, with the container runtime replaced by shell functions.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from tests.unit import chart_render
from tests.unit.k3s_recipe import (
    PROBE_TAG,
    REPO,
    buildable_stems,
    dumps,
    fresh_host_template_args,
    logical_commands,
    make_value,
    rendered_images,
    split_reference,
)


@pytest.fixture(scope="module")
def first_party(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, str, str, str, str]]:
    """`(owner, image, registry, stem, tag)` for every first-party image the fresh install renders."""
    docs = chart_render.render(*fresh_host_template_args(tmp_path_factory.mktemp("fresh-host"), TAG))
    stems = buildable_stems()
    found = []
    for owner, image in rendered_images(docs):
        registry, stem, tag = split_reference(image)
        if stem in stems:
            found.append((owner, image, registry, stem, tag))
    return found


#: Every check below runs at PROBE_TAG (see its note): at the default `dev` it could not tell a recipe
#: that states the tag from one that silently inherits the chart's identical default.
TAG = {"K3S_TAG": PROBE_TAG}


def _executed(target: str, prelude: str) -> str:
    """Run `target`'s recipe as make prints it, after `prelude` has defined shell functions that stand
    in for the container runtime and the cluster. Returns what the stand-ins printed."""
    script = prelude + "\n" + "\n".join(logical_commands(target, variables=TAG))
    done = subprocess.run(["bash", "-c", script], cwd=REPO, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert done.returncode == 0, f"`make {target}` failed under stand-ins: rc={done.returncode} stderr={done.stderr!r}"
    return done.stdout


def test_the_walk_sees_the_first_party_images(first_party: list[tuple[str, str, str, str, str]]) -> None:
    """A classifier that matched nothing would make every assertion below vacuously true."""
    seen = {stem for *_, stem, _ in first_party}

    assert {"gateway", "lance-rest-catalog", "ray-cluster", "web-home"} <= seen, f"the walk is blind to known images: {sorted(seen)}"


def test_the_local_tag_is_declared() -> None:
    assert make_value("$(K3S_TAG)") == "dev", "K3S_TAG must default to `dev`, the tag every side-loaded image on an existing node carries"


def test_every_first_party_image_a_fresh_install_asks_for_is_one_k3s_build_makes(first_party: list[tuple[str, str, str, str, str]]) -> None:
    built = set(make_value("$(K3S_IMAGES)", TAG).split())

    unbuilt = sorted({(stem, owner) for owner, _, _, stem, _ in first_party if stem not in built})

    assert not unbuilt, (
        "a fresh `make k3s-up` renders these first-party images, but `make k3s-build` / `make k3s-import` "
        f"never produce them, so their pods can only ImagePullBackOff: {unbuilt}. Add the stem to the "
        "Makefile's build set (COMPOSE_IMAGES / K3S_IMAGES)."
    )


def test_every_first_party_image_is_the_side_loaded_name_at_the_local_tag(first_party: list[tuple[str, str, str, str, str]]) -> None:
    tag = make_value("$(K3S_TAG)", TAG)

    wrong = [(owner, image) for owner, image, registry, _, got in first_party if registry or got != tag]

    assert not wrong, (
        f"a fresh `make k3s-up` must render every first-party image as the bare `<stem>:{tag}` that "
        f"`make k3s-import` side-loads; these name something else:\n{dumps(wrong)}"
    )


def test_the_build_makes_the_whole_set_at_the_local_tag() -> None:
    tag = make_value("$(K3S_TAG)", TAG)
    expected = {f"{image}:{tag}" for image in make_value("$(K3S_IMAGES)", TAG).split()}
    # `bash scripts/dagger-image.sh ... --tag <ref>` is the build; print the ref instead of building it.
    prelude = 'bash() { while [ $# -gt 0 ]; do [ "$1" = --tag ] && echo "$2"; shift; done; }'

    built = set(_executed("k3s-build", prelude).split())

    assert built == expected, f"`make k3s-build` builds {sorted(built)}; the deploy expects {sorted(expected)}"


def test_the_import_side_loads_the_whole_set_at_the_local_tag() -> None:
    tag = make_value("$(K3S_TAG)", TAG)
    expected = {f"{image}:{tag}" for image in make_value("$(K3S_IMAGES)", TAG).split()}
    # `docker save <ref> | sudo k3s ctr images import -` is the import. The saved ref goes to fd 3, past
    # the pipe the stand-in `sudo` drains; the recipe's own progress echo is silenced.
    prelude = 'exec 3>&1\ndocker() { [ "$1" = save ] && printf "%s\\n" "$2" >&3; }\nsudo() { cat >/dev/null; }\necho() { :; }'

    imported = set(_executed("k3s-import", prelude).split())

    assert imported == expected, f"`make k3s-import` side-loads {sorted(imported)}; the deploy expects {sorted(expected)}"


def test_kind_loads_the_whole_set_at_the_local_tag() -> None:
    """The kind path side-loads the same set, so it states the same tag rather than a literal."""
    tag = make_value("$(K3S_TAG)", TAG)
    expected = {f"{image}:{tag}" for image in make_value("$(K3S_IMAGES)", TAG).split()}
    [load] = [c for c in logical_commands("kind-load", variables=TAG) if "load docker-image" in c]

    loaded = {token for token in load.split() if ":" in token and not token.startswith(("-", "/"))}

    assert loaded == expected, f"`make kind-load` side-loads {sorted(loaded)}; the deploy expects {sorted(expected)}"


def test_the_classifier_reads_the_build_definitions_not_the_list_under_test() -> None:
    """`buildable_stems` must not be derived from `$(K3S_IMAGES)`, or the first assertion above could
    never fail: an image missing from the list would be missing from the classifier too."""
    source = pathlib.Path(REPO / "tests" / "unit" / "k3s_recipe.py").read_text(encoding="utf-8")
    body = source[source.index("def buildable_stems") : source.index("def split_reference")]

    assert "make_value" not in body and "K3S_IMAGES)" not in body.split('"""')[-1], "buildable_stems reads the list it is meant to check"
