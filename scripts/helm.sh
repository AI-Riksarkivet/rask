#!/usr/bin/env bash
# The ONE seam every helm call goes through — the same role scripts/dagger-image.sh plays for builds.
#
# WHY THIS EXISTS. The rask release lives in POSTGRES, not in a Kubernetes Secret, since 2026-08-15:
# helm embeds the whole chart in every revision and ~880 KB of chart/charts/*.tgz is already-compressed
# archives gzip cannot shrink, so the release secret crossed Kubernetes' hard 1 MiB object limit
# (v28 964 KB -> v35 1,048.5 KB against a 1,024 KB ceiling) and NO upgrade could be stored at all.
#
# THE HAZARD THIS GUARDS, and it is not "helm errors". A helm invocation without HELM_DRIVER=sql reads
# the EMPTY Secret backend, concludes the release is absent, and — since the deploy targets all use
# `upgrade --install` — INSTALLS OVER A LIVE ESTATE instead of upgrading it. Nothing fails; it simply
# answers from the wrong store. That is why this script exits rather than falling through: a silent
# success against the wrong backend is worse than any error.
#
# THE REAL FIX IS STILL SPLITTING THE CHART (infra vs app), which would let the app release fit the
# Secret backend again and delete this file. See open_tasks.md item 2.
set -euo pipefail

# Read-only subcommands never touch the release store, and REQUIRING a reachable database for them
# would break `make k3s-install` / `make bootstrap` on a host that has no cluster yet.
case "${1:-}" in
  template|lint|repo|dependency|dep|show|version|env|create|package|search|pull|plugin|completion|help|"")
    exec helm "$@"
    ;;
esac

: "${KUBECONFIG:=/etc/rancher/k3s/k3s.yaml}"
export KUBECONFIG

# An operator who has already exported a DSN owns the choice; do not second-guess it.
if [[ -n "${HELM_DRIVER_SQL_CONNECTION_STRING:-}" ]]; then
  export HELM_DRIVER="${HELM_DRIVER:-sql}"
  exec helm "$@"
fi

# The POD ip, not the Service: the ClusterIP is not routable from the host where helm runs, and it
# changes on restart, so it is derived per call rather than cached anywhere.
# THE RULE IS "use the driver that actually HOLDS this release", not "prefer SQL". Anything simpler
# is wrong for at least one real environment, and both were hit:
#
#   * "always require SQL" breaks a FRESH INSTALL and CI. `scripts/ray_e2e_stack.sh` installs the
#     chart into an empty kind cluster — where the Postgres this driver needs does not exist yet,
#     because the chart itself creates it. Requiring the database would make the install that creates
#     the database impossible.
#   * "use SQL whenever the AGE pod exists" is wrong the other way: a kind cluster gets an AGE pod as
#     soon as the chart lands, but ITS release lives in the Secret store, and switching mid-stream
#     would report that release absent.
#
# So the probe asks the SQL store whether it holds anything. A store with releases in it is the store
# in use; an unreachable or empty one means this release is not there and the default driver is right.
AGE_POD="${RASK_AGE_POD:-rask-age-0}"
AGE_IP="$(kubectl get pod "$AGE_POD" -o jsonpath='{.status.podIP}' 2>/dev/null || true)"

if [[ -z "$AGE_IP" ]]; then
  # No Postgres reachable => no SQL-backed release can exist => the Secret driver is authoritative.
  exec helm "$@"
fi

# The password rides ~/.pgpass (mode 600), never the DSN — a credential on a command line lands in
# the process table, shell history and any CI log that echoes the command.
AGE_USER="${RASK_AGE_USER:-lance}"
AGE_DB="${RASK_HELM_DB:-helm}"
PGPASS="${PGPASSFILE:-$HOME/.pgpass}"
if ! grep -qs "^${AGE_IP}:5432:${AGE_DB}:${AGE_USER}:" "$PGPASS" 2>/dev/null; then
  umask 077
  printf '%s:5432:%s:%s:%s\n' "$AGE_IP" "$AGE_DB" "$AGE_USER" "${RASK_AGE_PASSWORD:-lance}" >> "$PGPASS"
  chmod 600 "$PGPASS"
fi

DSN="postgresql://${AGE_USER}@${AGE_IP}:5432/${AGE_DB}?sslmode=disable"

# Does the SQL store actually hold a release? `helm list -aq` under the driver answers without needing
# any knowledge of the schema. Empty (or erroring) means this release is not there — pass through, and
# say so, because a silent switch in either direction is the failure mode this file exists to prevent.
if [[ -z "$(HELM_DRIVER=sql HELM_DRIVER_SQL_CONNECTION_STRING="$DSN" helm list -aq 2>/dev/null)" ]]; then
  echo ">> helm: SQL release store reachable but EMPTY — using the default driver for '$1'." >&2
  exec helm "$@"
fi

export HELM_DRIVER=sql
export HELM_DRIVER_SQL_CONNECTION_STRING="$DSN"
exec helm "$@"
