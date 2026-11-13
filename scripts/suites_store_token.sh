# The live suites' own store identity ([[XC-081]]), sourced by both runners (scripts/e2e_live.sh, scripts/e2e_stack.sh).
#
# suites_store_token <release>: prints a two-hour TokenRequest for `rask-e2e-suites` (deploy/values-e2e-suites.yaml) at
# the audience the release's store trusts (`objectStore.identity.audience`, read off the release's own values), which a
# suite exchanges at the store's STS for a session under that identity's policy. No suite holds the store's root pair.
# Fails, printing nothing, when the release names no audience or the account cannot be issued a token.
suites_store_token() {
  local release="$1" audience
  audience="$("$(dirname "${BASH_SOURCE[0]}")/helm.sh" get values "$release" --all -o json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["objectStore"]["identity"]["audience"])')" || return 1
  [ -n "$audience" ] || return 1
  kubectl create token rask-e2e-suites --audience "$audience" --duration 2h
}
