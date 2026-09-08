#!/usr/bin/env bash
# The ONE seam every helm call goes through — the same role scripts/dagger-image.sh plays for builds.
#
# WHY THIS EXISTS. Helm embeds the whole chart in every revision, and ~880 KB of chart/charts/*.tgz is
# already-compressed archives gzip cannot shrink, so on 2026-08-15 the release SECRET crossed
# Kubernetes' hard 1 MiB object limit (v28 964 KB -> v35 1,048.5 KB against a 1,024 KB ceiling) and no
# upgrade could be stored at all. The release moved to POSTGRES.
#
# THE SECRET STORE IS AUTHORITATIVE AGAIN — owner ruling 2026-09-08, after both stores were found
# holding `rask` and disagreeing (SQL rev 42, 2026-08-28; Secret rev 108, 2026-09-07). Everything since
# late August was written to the Secret store, so the SQL rows are stale residue and upgrading from
# them would roll the fleet back ten days of chart state.
#
# IT FITS TODAY AND THE MARGIN IS NOT LARGE, measured off the live objects rather than assumed — the
# `data` field is base64 in the JSON view, so a size read from `kubectl get -o json` overstates the
# stored object by a third:
#
#     v99  834.7 KB raw    v108  895.4 KB raw    ceiling 1,024 KB
#     ~6.7 KB per revision -> roughly 19 revisions of headroom
#
# So this is a reprieve, not a resolution, and every KB added to `chart/` spends it: helm stores the
# TEMPLATES too, so prose in a template counts against the same ceiling as a rendered manifest.
#
# THE REAL FIX IS STILL SPLITTING THE CHART (infra vs app), which is what would make the headroom
# stop mattering. The decision not to split it now, with the four measured alternatives and the
# conditions that reopen it, is in docs/DECISIONS.md.
#
# THE HAZARD THIS STILL GUARDS is answering from the wrong store. Every deploy target uses
# `upgrade --install`, so a call that reads a store which does not hold the release concludes it is
# absent and INSTALLS OVER A LIVE ESTATE. Nothing fails; it simply answers from the wrong history.
#
# THE REAL FIX IS STILL SPLITTING THE CHART (infra vs app), which would let the app release fit the
# Secret backend again and delete this file. The decision NOT to split it now — with the four
# measured alternatives and the conditions that reopen it — is in docs/DECISIONS.md.
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
SQL_RELEASES="$(HELM_DRIVER=sql HELM_DRIVER_SQL_CONNECTION_STRING="$DSN" helm list -aq 2>/dev/null || true)"
if [[ -z "$SQL_RELEASES" ]]; then
  echo ">> helm: SQL release store reachable but EMPTY — using the default driver for '$1'." >&2
  exec helm "$@"
fi

# BOTH STORES POPULATED IS NOT A CHOICE THIS SCRIPT MAY MAKE. The rule above — "a store with releases
# in it is the store in use" — is only sound while exactly ONE store has them. Measured 2026-09-08:
# SQL held rask rev 42 (Aug 28) while the Secret store held rev 108 (Sep 7, written 24 h earlier), so
# the probe selected a release ten days stale and an `upgrade` would have rewritten the whole fleet
# from that manifest. That is the same class of harm the header describes, arrived at from the other
# direction: not "installs over a live estate", but "upgrades it from the wrong history".
#
# A READ still answers, because refusing to answer makes the divergence harder to diagnose than it
# already is — it just says which store it came from. A MUTATION refuses: picking one silently is the
# thing this file exists to prevent, and only a person knows which history is the real one.
SECRET_RELEASES="$(helm list -aq 2>/dev/null || true)"
if [[ -n "$SECRET_RELEASES" ]]; then
  # BOTH STORES HOLD ONE, and the owner ruled which is real (2026-09-08): the Secret store. Said out
  # loud on every call rather than silently, because "both stores hold a release" is the state that
  # produced a ten-day-stale upgrade, and an operator who sees this line and disagrees needs to stop.
  echo ">> helm: both stores hold a release — using the SECRET store (owner ruling 2026-09-08; SQL rows are residue)." >&2
  echo "   SQL/Postgres : $(echo "$SQL_RELEASES" | tr '\n' ' ')" >&2
  echo "   Secret store : $(echo "$SECRET_RELEASES" | tr '\n' ' ')" >&2
  unset HELM_DRIVER HELM_DRIVER_SQL_CONNECTION_STRING
  exec helm "$@"
fi

# Only SQL holds it — the pre-ruling estate, or one restored from that history.
export HELM_DRIVER=sql
export HELM_DRIVER_SQL_CONNECTION_STRING="$DSN"
exec helm "$@"
