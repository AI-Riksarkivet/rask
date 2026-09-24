#!/usr/bin/env python3
"""Every image a chart render schedules that the NODE must already hold, and the stem that builds it.

`image.localImages=true` makes `rask.image` emit a bare `<component>:<tag>`, which containerd resolves
against Docker Hub when the kubelet does not already hold it: `pull access denied, repository does not
exist`, and the pod sits in ImagePullBackOff forever. So a kind stack has to build and `kind load`
exactly the set its OWN overlay schedules, and that set must be derived from the render — a list kept
beside the script falls behind the day a service is added (measured 2026-09-24 on `e2e-stack`: the
overlay scheduled seven rask images and the script built one).

PARSE THE DOCUMENTS, NEVER MATCH THE TEXT. Chart call sites pass `rask.image` through `quote`, so a
reference renders as `image: "gateway:dev"` — a filter anchored on an unquoted scalar matches nothing
at all. Measured the same day against the `e2e-stack` overlay: fifteen `:dev` references in the render,
zero matches, and because a stack that finds no image aborts rather than deploying, BOTH live lanes
died there before they reached a single assertion. Parsing costs milliseconds and cannot be wrong about
quoting, indentation, or whether the key sits under `containers`, `initContainers` or a CRD's own spec.

A BARE REFERENCE IS NOT ENOUGH ON ITS OWN — THE TAG DECIDES. `busybox:1.36` and `nats:2.14.2-alpine`
are bare too and pull from Docker Hub perfectly well; what marks an image as this repo's is that it
carries the tag the chart was rendered with. Hence `--tag`, which the caller passes the same value it
gave `image.tag`.

Emits `<image> <stem>` so the caller needs to know neither rule: the stem is what
`scripts/dagger-image.sh --name` takes, and `lance-rest-catalog` -> `rest-catalog` is the only place a
rendered name and its `.docker/<stem>.dockerfile` differ anywhere in the estate.

Usage:  helm template ... | uv run python scripts/side_loaded_images.py --tag dev
Pinned by `tests/unit/test_a_stack_builds_every_image_it_side_loads.py`, which imports the two
functions below so the gate and the stack scripts cannot hold different answers.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Iterator

import yaml


#: A side-loaded reference: a bare name, no registry path and no digest.
_BARE = re.compile(r"^[a-z0-9][a-z0-9-]*:[A-Za-z0-9][A-Za-z0-9._-]*$")

#: The prefix a rendered image name carries that its dockerfile stem does not.
_STEM_PREFIX = "lance-"


def dockerfile_stem(image: str) -> str:
    """The `.docker/<stem>.dockerfile` that builds this rendered image name."""
    return image.split(":")[0].removeprefix(_STEM_PREFIX)


def _images(node: object) -> Iterator[str]:
    """Every `image:` scalar anywhere in a manifest, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "image" and isinstance(value, str):
                yield value
            else:
                yield from _images(value)
    elif isinstance(node, list):
        for value in node:
            yield from _images(value)


def side_loaded_in(docs: Iterable[object], tag: str) -> list[str]:
    """The sorted, unique `<component>:<tag>` references these parsed documents schedule."""
    suffix = f":{tag}"
    return sorted({image for doc in docs for image in _images(doc) if image.endswith(suffix) and _BARE.match(image)})


def side_loaded(rendered: str, tag: str) -> list[str]:
    """The same, from Helm's raw stdout."""
    return side_loaded_in(yaml.safe_load_all(rendered), tag)


def main() -> int:
    parser = argparse.ArgumentParser(description="Side-loaded images in a chart render, read from stdin.")
    parser.add_argument("--tag", required=True, help="the value the render was given for image.tag")
    args = parser.parse_args()
    for image in side_loaded(sys.stdin.read(), args.tag):
        print(f"{image} {dockerfile_stem(image)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
