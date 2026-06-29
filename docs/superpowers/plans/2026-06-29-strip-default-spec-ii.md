# Strip Platform to Front-Door Only (Spec II) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Decommission the legacy single-tenant default stack so the `rask` platform install is front-door only (picker + controlplane + gateway + Kueue cohort + operators), without breaking per-project provisioning.

**Architecture:** Add a `singleTenant.enabled: false` workload gate to `chart/`; gate the single-tenant workload templates on it while leaving the operator subcharts enabled via their own `.enabled`. Single repo (`/home/morgan/rask`, branch `feat/strip-default`).

**Tech Stack:** Helm.

**Spec:** `docs/superpowers/specs/2026-06-29-strip-default-spec-ii-design.md`

## Global Constraints

- The cnpg/rustfs/kuberay/kueue/dapr/openfga **operator subcharts MUST stay enabled** (per-project stacks depend on them). Never set their `.enabled` false.
- Keep the front door: `home` (picker), `controlplane`, `gateway`, `kueue-queues` (the cohort).
- `singleTenant.enabled` default **false** (front-door only out of the box; `true` restores the legacy single-tenant stack).
- Verify with `helm template`; the live deploy is **destructive** to the running `default` backend (separate checkpointed task).
- Commits: conventional, **no `Co-Authored-By: Claude` trailer**.

## File Structure

Modified (all under `/home/morgan/rask/chart`):
- `values.yaml` — add `singleTenant.enabled: false`; set `nats.enabled: false`.
- `templates/cnpg-cluster.yaml`, `templates/rustfs-tenant.yaml`, `templates/rayservice.yaml`, `templates/migration-job.yaml` — add `singleTenant` to the existing guard.
- `templates/fleet.yaml` — render only `gateway` unless `singleTenant`.
- `templates/frontends.yaml` — render only `catchAll` (home) unless `singleTenant`.
- `templates/ingress.yaml` — gate the `/default/<domain>` routes on `singleTenant`.

---

### Task 1: Add the `singleTenant` workload gate

**Files:** the 8 chart files above.

**Interfaces:**
- Produces: `helm template` with default values renders the front-door set only; `--set singleTenant.enabled=true` restores the legacy set.

- [ ] **Step 1: Add the values**

In `/home/morgan/rask/chart/values.yaml`, add a top-level block (near the top, e.g. after `serviceAccount:`):
```yaml
# singleTenant gates the LEGACY single-tenant default workloads (the default
# domain services + /default frontends + default Postgres/object-store/Ray/NATS/
# migration). Default false => front-door-only install (picker + controlplane +
# gateway + operators). Set true to restore the old single-tenant stack.
# NOTE: this is separate from the operator subchart toggles (cnpg/rustfs/...),
# which stay enabled so per-project stacks keep working.
singleTenant:
  enabled: false
```
and set the existing `nats:` block's `enabled` to `false`:
```yaml
nats:
  enabled: false   # platform NATS was single-tenant; per-project renders its own
```
(grep `grep -n "^nats:" -A1 chart/values.yaml` to find the exact line; change only that `enabled`.)

- [ ] **Step 2: Gate the four singly-guarded workload templates**

Change the top-level `{{- if ... }}` guard in each (operator subchart stays via its own `.enabled`):
- `chart/templates/cnpg-cluster.yaml`: `{{- if .Values.cnpg.enabled }}` → `{{- if and .Values.cnpg.enabled .Values.singleTenant.enabled }}`
- `chart/templates/rustfs-tenant.yaml`: `{{- if .Values.rustfs.enabled }}` → `{{- if and .Values.rustfs.enabled .Values.singleTenant.enabled }}`
- `chart/templates/rayservice.yaml`: `{{- if .Values.ray.enabled }}` → `{{- if and .Values.ray.enabled .Values.singleTenant.enabled }}`
- `chart/templates/migration-job.yaml`: `{{- if .Values.migrations.enabled }}` → `{{- if and .Values.migrations.enabled .Values.singleTenant.enabled }}`

- [ ] **Step 3: Gate fleet services to gateway-only**

In `chart/templates/fleet.yaml`, the body is `{{- range $name, $svc := .Values.services }}` (line 3) … `{{- end }}` (end of file). Immediately after the `range` line, open a guard, and add its `{{- end }}` immediately before the loop's closing `{{- end }}`:
```yaml
{{- range $name, $svc := .Values.services }}
{{- if or (eq $name "gateway") $root.Values.singleTenant.enabled }}
```
…and before the final `{{- end }}` that closes the `range`, add a matching `{{- end }}` (so the structure is `range` → `if` → (Deployment+Service+`---`) → `end` (if) → `end` (range)). `$root` is already defined at line 1.

- [ ] **Step 4: Gate frontends to catch-all (home) only**

In `chart/templates/frontends.yaml`, the body is `{{- range $fe.apps }}` (line 9) … `{{- end }}` (line 89). Right after the `range` line add:
```yaml
{{- range $fe.apps }}
{{- if or .catchAll $root.Values.singleTenant.enabled }}
```
and add a matching `{{- end }}` immediately before the loop's closing `{{- end }}`. `$root` is defined at line 7.

- [ ] **Step 5: Gate the `/default` ingress routes**

In `chart/templates/ingress.yaml`, the domain-route loop guards each path with `{{- if not .catchAll }}` (line 37). Change it to also require singleTenant:
```yaml
          {{- if and (not .catchAll) $.Values.singleTenant.enabled }}
```
(Leave the `/api` rule at line 27 and the catch-all `/` loop at lines 48-58 unchanged — those are the front door.)

- [ ] **Step 6: Verify the front-door render (default values)**

Run:
```bash
cd /home/morgan/rask
helm template rask ./chart > /tmp/fd.yaml 2>&1 && echo "render OK"
echo "--- present (front door) ---"
for n in rask-home rask-controlplane rask-gateway; do grep -q "name: $n$" /tmp/fd.yaml && echo "ok $n" || echo "MISSING $n"; done
grep -q "kind: ClusterQueue" /tmp/fd.yaml && echo "ok kueue cohort"
echo "--- absent (single-tenant) ---"
for n in rask-core-api rask-search-api rask-volumes-api rask-ray-api rask-orchestrator rask-overview rask-storage; do grep -q "name: $n$" /tmp/fd.yaml && echo "STILL PRESENT $n" || echo "gone $n"; done
grep -q "kind: Cluster" /tmp/fd.yaml && echo "cnpg Cluster STILL PRESENT" || echo "gone cnpg Cluster"
grep -q "kind: Tenant" /tmp/fd.yaml && echo "rustfs Tenant STILL PRESENT" || echo "gone rustfs Tenant"
grep -q "kind: RayService" /tmp/fd.yaml && echo "RayService STILL PRESENT" || echo "gone RayService"
grep -q "path: /default/" /tmp/fd.yaml && echo "/default routes STILL PRESENT" || echo "gone /default routes"
```
Expected: front-door names present + kueue cohort; all single-tenant names/kinds/`/default` routes **gone**.

- [ ] **Step 7: Verify the legacy set still renders when explicitly enabled (no template breakage)**

Run:
```bash
helm template rask ./chart --set singleTenant.enabled=true --set nats.enabled=true > /tmp/st.yaml 2>&1 && echo "render OK"
for n in rask-core-api rask-overview; do grep -q "name: $n$" /tmp/st.yaml && echo "ok $n restored" || echo "MISSING $n"; done
grep -q "path: /default/overview" /tmp/st.yaml && echo "ok /default routes restored"
```
Expected: with `singleTenant.enabled=true`, the legacy services/frontends/`/default` routes render again (proves the gates are correct, not destructive to the template).

- [ ] **Step 8: Verify operators are NOT gated off**

Run:
```bash
grep -c "cloudnative-pg\|cnpg" /tmp/fd.yaml; echo "(cnpg operator artifacts should be > 0 in front-door render)"
helm template rask ./chart | grep -iE "kind: Deployment" | grep -iE "cnpg|rustfs|kuberay|kueue|dapr" | head
```
Expected: operator Deployments still present in the front-door render (their `.enabled` untouched).

- [ ] **Step 9: Commit**

```bash
cd /home/morgan/rask
git add chart/values.yaml chart/templates/cnpg-cluster.yaml chart/templates/rustfs-tenant.yaml chart/templates/rayservice.yaml chart/templates/migration-job.yaml chart/templates/fleet.yaml chart/templates/frontends.yaml chart/templates/ingress.yaml
git commit -m "feat(chart): singleTenant gate — front-door-only install (decommission default stack)"
```

---

### Task 2: Live decommission on k3s (destructive — controller-run, checkpointed)

**Files:** none (deploy + verify).

- [ ] **Step 1: Snapshot + upgrade with singleTenant off**

```bash
cd /home/morgan/rask
helm get values rask -n default -o yaml > /tmp/rask-values-preStripII.yaml
make k3s-up   # chart now defaults singleTenant.enabled=false, nats.enabled=false
```

- [ ] **Step 2: Confirm the legacy backend is gone, operators remain**

```bash
echo "--- single-tenant workloads (expect gone) ---"
kubectl -n default get deploy | grep -E "rask-(core-api|search-api|volumes-api|ray-api|orchestrator|overview|storage|compute|discover|train|studio)" || echo "gone"
kubectl -n default get cluster.postgresql.cnpg.io,tenant.rustfs.com,rayservice.ray.io 2>/dev/null || echo "gone (default CRs)"
echo "--- operators (expect Running) ---"
kubectl get deploy -A | grep -iE "cnpg|cloudnative-pg|rustfs-operator|kuberay-operator|kueue|dapr" | grep -v "0/"
```
Expected: single-tenant workloads gone; operator controllers Running.

- [ ] **Step 3: Confirm the front door + per-project still work**

```bash
kubectl -n default get deploy | grep -E "rask-(home|controlplane|gateway)"
kubectl -n default exec deploy/rask-gateway -c gateway -- python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8888/api/projects/').read().decode())"
NODE_IP=10.16.51.56
curl -s -L --resolve demo.localhost:80:$NODE_IP -o /dev/null -w "demo.localhost/default/overview -> HTTP %{http_code}\n" http://demo.localhost/default/overview
```
Expected: home/controlplane/gateway Running; `/api/projects/` returns `demo` with its url; `demo.localhost/default/overview` → 200 (per-project unaffected).

- [ ] **Step 4: Prove provisioning still works (operators survived)**

```bash
kubectl annotate project demo rask.io/rev="postStripII-$(date +%s)" --overwrite
sleep 15
kubectl get project demo -o jsonpath='phase={.status.phase}{"\n"}'
kubectl -n project-demo get cluster.postgresql.cnpg.io demo-postgres -o jsonpath='ready={.status.readyInstances}{"\n"}' 2>/dev/null
```
Expected: `demo` stays `Ready`; its CNPG cluster still has a ready instance (operators are alive and reconciling per-project resources).

- [ ] **Step 5: Record the result**

Note the outcome in the ledger. If anything per-project broke, an operator was wrongly gated — roll back (`helm rollback rask` / `helm get values …preStripII`) and fix the guard.

---

## Self-Review

**Spec coverage:** singleTenant gate (values + 7 templates) → Task 1; nats off → Task 1 Step 1; operators kept (only workload guards changed, subchart `.enabled` untouched) → Task 1 Steps 2-5 + 8; front-door-only render → Step 6; reversibility (singleTenant=true restores) → Step 7; live decommission + per-project-survives → Task 2. ✓ The `/default` URL cleanup is explicitly out of scope (Spec III). ✓

**Placeholder scan:** none — every guard change is an exact before→after string; verification uses concrete greps.

**Consistency:** `singleTenant.enabled` is the single new key, referenced identically across all 7 templates (`.Values.singleTenant.enabled`, or `$root.Values.singleTenant.enabled` / `$.Values.singleTenant.enabled` inside ranges where the context dot is reassigned). Operator toggles (`cnpg/rustfs/ray.enabled`) are kept in the workload guards via `and`, so the operators (gated by the subchart conditions on those same flags) stay on.
