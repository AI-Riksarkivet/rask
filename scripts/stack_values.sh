# The governed stack the live lanes install (scripts/e2e_stack.sh on kind, scripts/install_from_empty.sh on an empty
# cluster), sourced by both so they install one shape: auth ON, the #3-A/#3-B/#4 flags ON, maintenance ON, the
# telemetry chain OFF.
# Expects RELEASE; sets BASE_A, BASE_B, ADMIN_PRINCIPAL and HELM_SET.

# The two data-distribution buckets #3-B spreads fragments across (must be allowlisted in the chart AND
# physically provisioned — the allowlist is a governance gate, not a provisioner).
BASE_A="s3://mb-a"
BASE_B="s3://mb-b"
# THE ESTATE ADMIN IS THE IDENTITY THE SUITES DRIVE AS, named at install the way the live estate names it. Minting a
# project gates on the estate root, and only the bootstrap-admin hook writes that grant, so without it every suite that
# mints a tenant is refused 403 `can_observe_events required on estate:rask` before it asserts anything.
ADMIN_PRINCIPAL="$(uv run python "$(dirname "${BASH_SOURCE[0]}")/dex_principal.py" alice@example.com)"
# shellcheck disable=SC2034 # read by the sourcing script
HELM_SET=(
  --set image.localImages=true
  # The bus the nats fixture runs ([[XC-129]]).
  --set-string nats.url=nats://rask-nats:4222
  --set auth.enabled=true
  # The dex fixture's issuer at its default, the in-cluster one: the lanes reach it as `rask-dex`, as every door does.
  --set-string auth.oidc.issuer=http://rask-dex:5556/dex
  --set-string "auth.bootstrapAdmin=$ADMIN_PRINCIPAL"
  --set catalog.warehouses.enabled=true
  --set-json "catalog.multibase.dataBases=[\"$BASE_A\",\"$BASE_B\"]"
  --set services.lineage.outbox.enabled=true
  --set services.lineage.relay.enabled=true
  --set observability.enabled=false
  # Maintenance is a lakehouse component: the e2e lane drives its deployed sweep (tests/e2e-py/test_maintenance_e2e.py).
  --set maintenance.enabled=true
  # The lanes drive each service directly, not through an edge, and a lane cluster runs no Gateway.
  --set route.enabled=false
)
