"""Stand-ins for the `bao` and `nk` CLIs, for the tests that run the scripts the chart renders and the operator's own.

A shared plain module, as `chart_render.py` is, so two tests that run a script against the same CLI contract exercise one
stand-in instead of two that drift. Both keep every call they receive and fail loudly on one the scripts have no business
making, so a script that starts calling a tool nobody measured goes red instead of passing against a double that shrugs.
"""

from __future__ import annotations

from pathlib import Path


#: Answers by BAO_ADDR from two directories of `key=value` files, `$STORES/local` for 127.0.0.1:8200 and `$STORES/served` only
#: at $SERVICE_ADDR. A store holding `<name>.refused` refuses the connection, `<name>.forbidden` answers 403, and
#: `local.readonly` fails every write; the messages are OpenBao 2.2.0's, measured on the estate 2026-10-02. A `key=@file` argument
#: stores the file's content, as the CLI does, and a subcommand the scripts have no business calling fails loudly.
BAO = """\
#!/bin/sh
case "$BAO_ADDR" in
  http://127.0.0.1:8200) name=local ;;
  "$SERVICE_ADDR") name=served ;;
  *) echo "Get \\"$BAO_ADDR/v1/sys/internal/ui/mounts/secret\\": dial tcp: lookup: no such host" >&2; exit 2 ;;
esac
store="$STORES/$name"
echo "$BAO_ADDR $*" >> "$STORES/calls"
[ "$1" = status ] && exit 0
if [ -e "$store.refused" ]; then
  echo "Get \\"$BAO_ADDR/v1/sys/internal/ui/mounts/secret\\": dial tcp 10.43.63.224:8200: connect: connection refused" >&2; exit 2
fi
if [ -e "$store.forbidden" ]; then printf 'Error making API request.\\n\\nCode: 403. Errors:\\n\\n* permission denied\\n' >&2; exit 2; fi
if [ "$1 $2" = "kv put" ]; then
  [ -e "$store.readonly" ] && { echo "Error writing data to $3" >&2; exit 2; }
  key=$3; shift 3
  mkdir -p "$(dirname "$store/$key")"
  for arg in "$@"; do
    case "$arg" in
      *=@*) printf '%s=%s\\n' "${arg%%=@*}" "$(cat "${arg#*=@}")" ;;
      *) printf '%s\\n' "$arg" ;;
    esac
  done > "$store/$key"
  exit 0
fi
if [ "$1 $2" = "kv get" ]; then
  field=${3#-field=}
  if [ -f "$store/$4" ] && line=$(grep "^$field=" "$store/$4"); then printf '%s\\n' "${line#*=}"; exit 0; fi
  echo "No value found at ${4%%/*}/data/${4#*/}" >&2; exit 2
fi
case "$1" in auth|write|policy) exit 0 ;; esac
echo "bao stand-in: unhandled call: $*" >&2; exit 99
"""

#: `nk -gen user -pubout` hands out the next generated pair from $NK_PAIRS (two lines: the seed, then the public key), fails on the
#: call numbers in NK_FAIL and prints one line of garbage on those in NK_GARBAGE. Any other call is a script calling a tool the
#: measurement did not cover.
NK = """\
#!/bin/sh
n=$(cat "$NK_STATE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$NK_STATE"
[ "$*" = "-gen user -pubout" ] || { echo "nk stand-in: unexpected call: $*" >&2; exit 99; }
case " $NK_FAIL " in *" $n "*) echo "nk: boom" >&2; exit 1 ;; esac
case " $NK_GARBAGE " in *" $n "*) echo "not a key"; exit 0 ;; esac
sed -n "$((2 * n - 1)),$((2 * n))p" "$NK_PAIRS"
"""


def install(directory: Path, **scripts: str) -> None:
    """Write each stand-in into `directory` as an executable named by its keyword."""
    for name, body in scripts.items():
        (directory / name).write_text(body)
        (directory / name).chmod(0o755)
