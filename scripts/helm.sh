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
AGE_POD="${RASK_AGE_POD:-rask-age-0}"
AGE_IP="$(kubectl get pod "$AGE_POD" -o jsonpath='{.status.podIP}' 2>/dev/null || true)"

if [[ -z "$AGE_IP" ]]; then
  cat >&2 <<EOF
!! helm release storage is UNREACHABLE — refusing to run '$1'.

   Could not read the IP of pod '$AGE_POD' (KUBECONFIG=$KUBECONFIG).

   This script will NOT fall back to helm's default Secret driver. The rask release lives in
   Postgres; against the Secret backend helm would report the release as ABSENT and
   'upgrade --install' would RE-INSTALL over a live estate without erroring.

   Fix the cluster connection, or set HELM_DRIVER_SQL_CONNECTION_STRING yourself if the database
   is reachable another way.
EOF
  exit 1
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

export HELM_DRIVER=sql
export HELM_DRIVER_SQL_CONNECTION_STRING="postgresql://${AGE_USER}@${AGE_IP}:5432/${AGE_DB}?sslmode=disable"
exec helm "$@"
