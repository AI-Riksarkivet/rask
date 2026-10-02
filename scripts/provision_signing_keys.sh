#!/usr/bin/env bash
# Create the event-signing keys a store that nothing seeds must hold ([[LH-064]]; chart values.yaml `signing:`,
# docs/OPERATORS.md "Event signing keys"). The render of a non-dev store (openbao.devMode=false or openbao.externalAddr)
# refuses until this has run for every identity it names and `--set signing.provisioned=true` says so.
#
#   scripts/provision_signing_keys.sh [--rotate] <identity>...
#
# Per identity: `nk -gen user -pubout`, then `bao kv put` of signing-public-<identity> (field `keys`, the public list) and
# only then signing-key-<identity> (field `seed`): publish before use, because a signer is Ready only while its kid is
# listed. An identity whose pair already exists is left alone. --rotate prepends a new key to the list and keeps one
# previous (rotate at most once per 7 days: the bus keeps 168 h), and writes the new seed last.
#
# No seed is printed or passed in an argument list: nk's output stays in shell variables and files in a 0700 directory
# that is removed on exit, and `bao kv put` reads each value from a file (`key=@file`).
#
# Environment: BAO_ADDR and BAO_TOKEN for the store (an operator token allowed to write secret/signing-*), NK and BAO to
# name the binaries (default nk, bao), SIGNING_KV_MOUNT for the KV v2 mount (default secret).
set -euo pipefail
umask 077

nk=${NK:-nk}
bao=${BAO:-bao}
mount=${SIGNING_KV_MOUNT:-secret}
rotate=0
if [ "${1:-}" = "--rotate" ]; then
  rotate=1
  shift
fi
[ "$#" -gt 0 ] || {
  echo "usage: $0 [--rotate] <identity>..." >&2
  exit 2
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

seed_re='^SU[A-Z2-7]{56}$'
pub_re='^U[A-Z2-7]{55}$'
keys_re='^U[A-Z2-7]{55}(,U[A-Z2-7]{55})?$'

# read_field <secret> <field>: sets `got` and returns 0 when present, returns 3 when the secret is absent, and stops on
# any other failure rather than writing over something it could not read.
read_field() {
  if got=$("$bao" kv get -field="$2" "$mount/$1" 2>&1); then
    return 0
  fi
  case "$got" in
    "No value found at"*)
      got=
      return 3
      ;;
  esac
  echo "cannot read $mount/$1: $got" >&2
  exit 1
}

# mint <identity>: a fresh pair as files $work/<identity>.seed and $work/<identity>.pub, taken by shape and never printed.
mint() {
  local out word s="" p=""
  out=$("$nk" -gen user -pubout) || {
    echo "nk failed for $1" >&2
    exit 1
  }
  set -f
  for word in $out; do
    if [[ $word =~ $seed_re ]]; then s=$word; elif [[ $word =~ $pub_re ]]; then p=$word; fi
  done
  set +f
  if [ -z "$s" ] || [ -z "$p" ]; then
    echo "nk printed no key pair for $1" >&2
    exit 1
  fi
  printf '%s' "$s" >"$work/$1.seed"
  printf '%s' "$p" >"$work/$1.pub"
}

# publish <identity> <list>: the public list first, then the seed it belongs to.
publish() {
  printf '%s' "$2" >"$work/$1.keys"
  "$bao" kv put "$mount/signing-public-$1" keys=@"$work/$1.keys" >/dev/null
  "$bao" kv put "$mount/signing-key-$1" seed=@"$work/$1.seed" >/dev/null
}

for id in "$@"; do
  [[ $id =~ ^[a-z0-9][a-z0-9-]*$ ]] || {
    echo "identity $id must match ^[a-z0-9][a-z0-9-]*$" >&2
    exit 2
  }
  if read_field "signing-key-$id" seed; then have_key=1; else have_key=0; fi
  got=
  if read_field "signing-public-$id" keys; then
    have_list=1
    list=$got
  else
    have_list=0
    list=
  fi
  if [ "$have_key" = 1 ] && [ "$have_list" = 0 ]; then
    echo "signing-key-$id exists without signing-public-$id: refusing to publish another list for it" >&2
    exit 1
  fi
  if [ "$have_list" = 1 ] && ! [[ $list =~ $keys_re ]]; then
    echo "signing-public-$id is malformed: refusing to write over it" >&2
    exit 1
  fi
  if [ "$rotate" = 0 ]; then
    if [ "$have_key" = 1 ]; then
      echo "signing pair for $id: exists, left alone"
      continue
    fi
    if [ "$have_list" = 1 ]; then
      echo "signing-public-$id exists without signing-key-$id: run with --rotate to mint a key and prepend it" >&2
      exit 1
    fi
    mint "$id"
    publish "$id" "$(cat "$work/$id.pub")"
    echo "signing pair for $id: created"
  else
    if [ "$have_list" = 0 ]; then
      echo "signing pair for $id: nothing to rotate" >&2
      exit 1
    fi
    mint "$id"
    publish "$id" "$(cat "$work/$id.pub"),${list%%,*}"
    echo "signing pair for $id: rotated"
  fi
done
