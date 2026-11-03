"""Pin a copy of the product chart to one lakehouse release ([[XC-187]]).

Writes the release's version into Chart.yaml (`version` and `appVersion`) and the registry and each published image's
digest into values.yaml's `image` defaults, so the packaged chart installs exactly the images it was released with
and a client names the whole lakehouse by one chart version. The values file is edited as text, line by line, so its
comments and `@schema` annotations survive; each edit must match exactly once, and the result is parsed back and
checked before it is written.

Usage: pin_chart_release.py <chart dir> --version X.Y.Z --repository <registry> --digest <image>=sha256:<hex> ...
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml


SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[a-z0-9.-]+(:\d+)?(/[a-z0-9._-]+)+$")


def _replace_once(text: str, pattern: str, replacement: str, what: str) -> str:
    new, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"{what}: expected exactly one match for {pattern!r}, found {count}")
    return new


def pin(chart: Path, version: str, repository: str, digests: dict[str, str]) -> None:
    """Rewrite ``chart`` in place for the release ``version`` of ``digests`` under ``repository``."""
    if not SEMVER.match(version):
        raise SystemExit(f"version {version!r} is not semver")
    if not REPOSITORY.match(repository):
        raise SystemExit(f"repository {repository!r} is not a registry path")
    for name, digest in digests.items():
        if not DIGEST.match(digest):
            raise SystemExit(f"digest of {name!r} is not sha256:<64 hex>: {digest!r}")

    chart_yaml, values_yaml = chart / "Chart.yaml", chart / "values.yaml"
    chart_text = _replace_once(chart_yaml.read_text(), r"^version: .*$", f"version: {version}", "Chart.yaml version")
    chart_text = _replace_once(chart_text, r"^appVersion: .*$", f'appVersion: "{version}"', "Chart.yaml appVersion")

    pinned = ", ".join(f'"{name}": "{digest}"' for name, digest in sorted(digests.items()))
    values_text = _replace_once(values_yaml.read_text(), r'^  repository: ""$', f'  repository: "{repository}"', "values.yaml image.repository")
    values_text = _replace_once(values_text, r"^  digests: \{\}( #.*)?$", f"  digests: {{{pinned}}}\\1", "values.yaml image.digests")
    image = yaml.safe_load(values_text)["image"]
    if image["repository"] != repository or image["digests"] != digests:
        raise SystemExit(f"values.yaml image block did not take the release: {image}")
    # Both files are written only once both edits are checked, so a refused pin leaves the chart as it was.
    chart_yaml.write_text(chart_text)
    values_yaml.write_text(values_text)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("chart", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--digest", action="append", required=True, metavar="IMAGE=sha256:HEX")
    args = parser.parse_args(argv)
    if malformed := [item for item in args.digest if "=" not in item]:
        raise SystemExit(f"--digest takes IMAGE=sha256:HEX, got {malformed}")
    digests = dict(item.split("=", 1) for item in args.digest)
    pin(args.chart, args.version, args.repository, digests)


if __name__ == "__main__":
    main(sys.argv[1:])
