# The pieces scripts/install_from_empty.sh and scripts/gitops_from_empty.sh share, sourced by both so an install by
# Helm and an install by Argo CD stand on the same fixtures and report a failure the same way. Run from the repo
# root, against the cluster KUBECONFIG names.

# install_stack_fixtures [<extra helm args for the dex fixture>...]: the development prerequisites and stand-ins from
# deploy/fixtures, each its own release, in the order rask's install needs them.
install_stack_fixtures() {
  scripts/fixtures.sh external-secrets dapr nats
  scripts/fixtures.sh dex "$@"
  scripts/fixtures.sh openbao
  # The object store runs the upstream RustFS image its fixture pins by digest, which the node pulls itself.
  scripts/fixtures.sh rustfs
}

# start_job_recorder <dir>: A Job that exhausts its backoff has its pod deleted, and that pod's log with it: record each
# Job pod's state and log (every container's, the attempt before a restart too) into <dir> while the install runs. Sets
# RECORDER to the recorder's PID.
start_job_recorder() {
  local dir="$1"
  ( while sleep 3; do
      for p in $(kubectl get pods -l batch.kubernetes.io/job-name -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        {
          kubectl get pod "$p" -o jsonpath='{range .status.initContainerStatuses[*]}{.name} restarts={.restartCount} last={.lastState.terminated.reason}/{.lastState.terminated.exitCode} now={.state.terminated.reason}/{.state.terminated.exitCode}{"\n"}{end}{range .status.containerStatuses[*]}{.name} restarts={.restartCount} last={.lastState.terminated.reason}/{.lastState.terminated.exitCode} now={.state.terminated.reason}/{.state.terminated.exitCode}{"\n"}{end}'
          for c in $(kubectl get pod "$p" -o jsonpath='{.spec.initContainers[*].name} {.spec.containers[*].name}'); do
            echo "[$c, previous attempt]"; kubectl logs "$p" -c "$c" --previous --tail=40 2>/dev/null || true
            echo "[$c]"; kubectl logs "$p" -c "$c" --tail=40 2>/dev/null || true
          done
        } > "$dir/$p.tmp" 2>&1 && mv "$dir/$p.tmp" "$dir/$p"
      done
    done ) &
  RECORDER=$!
}

# print_cluster_state <job log dir>: every pod's state, each Job and ExternalSecret, the events and logs of whatever pod
# is not Ready, and the Job pod logs the recorder kept.
print_cluster_state() {
  local dir="$1"
  kubectl get pods -A -o wide || true
  kubectl get jobs -o wide || true
  kubectl get externalsecrets -o custom-columns='NAME:.metadata.name,READY:.status.conditions[0].status,REASON:.status.conditions[0].reason,MESSAGE:.status.conditions[0].message' 2>/dev/null || true
  for p in $(kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[*].ready}{"\n"}{end}' | awk '/false/ || NF==1 {print $1}'); do
    echo "--- NOT READY: $p"
    kubectl describe pod "$p" 2>/dev/null | sed -n '/Events:/,$p' | head -15 || true
    kubectl logs "$p" --all-containers --tail=40 2>/dev/null || true
  done
  for f in "$dir"/*; do [ -f "$f" ] && { echo "--- JOB POD (recorded during the install): ${f##*/}"; cat "$f"; }; done
  return 0
}
