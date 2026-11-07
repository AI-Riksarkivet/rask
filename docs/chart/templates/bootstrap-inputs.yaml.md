# chart/templates/bootstrap-inputs.yaml

Rationale for `chart/templates/bootstrap-inputs.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: v1`

A CHANGED BOOTSTRAP INPUT IS ONE ARGO CD SEES ([[XC-164]]). Argo CD leaves its hooks out of the state it compares
(gitops-engine `pkg/sync/reconcile.go` `splitHooks`, read at v3.5.7), and every bootstrap Job is a hook there
(`lance.reconciledJob`), so a value that changed only a Job (the streams' max age, a store policy, a grant) would leave
the Application Synced and no auto-sync would run the Job that applies it. This plain ConfigMap holds, per bootstrap
template, a digest of its Jobs' specs: a changed input changes it, the Application goes OutOfSync, and the sync runs
every hook again. Under Helm every upgrade runs the hooks anyway, and the ConfigMap is inert.
`tests/unit/test_a_bootstrap_jobs_input_change_is_one_argo_cd_sees.py` holds that an input changes exactly its
template's digest and that every template rendering an Argo CD hook is digested here.
