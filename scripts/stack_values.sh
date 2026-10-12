# The governed stack the live lanes install (scripts/e2e_stack.sh on kind, scripts/install_from_empty.sh on an empty
# cluster), sourced by both so they install one shape: auth ON, the #3-A/#3-B/#4 flags ON, the heavy extras OFF.
# Expects RELEASE; sets BASE_A, BASE_B and HELM_SET.

# The two data-distribution buckets #3-B spreads fragments across (must be allowlisted in the chart AND
# physically provisioned — the allowlist is a governance gate, not a provisioner).
BASE_A="s3://mb-a"
BASE_B="s3://mb-b"
# shellcheck disable=SC2034 # read by the sourcing script
HELM_SET=(
  --set image.localImages=true
  # The bus the nats fixture runs ([[XC-129]]).
  --set-string nats.url=nats://rask-nats:4222
  --set auth.enabled=true
  --set medallion.fgaEnabled=true
  # The ray-OFF core lane (the Ray path's home is ray_e2e_stack.sh): with the medallion's Ray lane on and
  # no Ray named, the chart refuses the render ([[CP-041]]).
  --set medallion.ray=false
  --set catalog.warehouses.enabled=true
  --set-json "catalog.multibase.dataBases=[\"$BASE_A\",\"$BASE_B\"]"
  --set services.lineage.outbox.enabled=true
  --set services.lineage.reconcile.enabled=true
  --set observability.enabled=false
  --set maintenance.enabled=false
  --set frontend.enabled=false
)
