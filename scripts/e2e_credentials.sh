# The credentials a live run hands its suites ([[XC-202]]), sourced by scripts/e2e_live.sh and scripts/e2e_stack.sh so
# both write them one way. EVERY CREDENTIAL IS A FILE, NEVER AN EXPORT: each is written to its own file in a directory only
# this user reads, the suites read it there (`tests/e2e-py/credentials.py`), and the run exports nothing but the directory,
# as LANCE_E2E_CREDENTIALS. A variable is inherited by every process the run starts and listed by anything that reads its
# /proc entries.
# Sets CREDENTIALS; the sourcing script removes the directory when it exits.

CREDENTIALS="$(mktemp -d)"

# keep NAME VALUE: writes the credential NAME, readable by this user alone. printf is a builtin, so VALUE reaches no argv.
keep() { (umask 077; printf '%s' "$2" > "$CREDENTIALS/$1"); }

# authorized TOKEN CURL-ARGS...: curl with `authorization: Bearer TOKEN`, the header read on stdin (`-H @-`), so the token
# reaches no argv. A request body is passed with -d, never on stdin.
authorized() { local token="$1"; shift; printf 'authorization: Bearer %s\n' "$token" | curl -H @- "$@"; }

# password_grant TOKEN-URL CLIENT SECRET SCOPE USERNAME PASSWORD: the identity token Dex issues for a password grant, its
# form read on stdin (`--data @-`), so neither the client's secret nor the password reaches an argv.
password_grant() {
  printf 'grant_type=password&client_id=%s&client_secret=%s&scope=%s&username=%s&password=%s' "$2" "$3" "$4" "$5" "$6" \
    | curl -s -m 20 "$1" --data @- \
    | uv run python -c "import sys,json;print(json.load(sys.stdin).get('id_token',''))"
}
