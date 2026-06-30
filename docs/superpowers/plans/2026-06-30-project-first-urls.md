# Project-first URLs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop the vestigial `/default/` URL segment so per-project pages live at `<project>.host/<domain>` (host carries the project, path carries the domain), with bare `<project>.host/` redirecting to `/overview`.

**Architecture:** The SvelteKit base becomes a static `/<domain>` (was `/default/<domain>`) — still build-time, still one shared image per MFE. The shared `@rask/ui` nav drops the project path-segment (hrefs become `/<domain>`); the apps' base + all `/default/` literals collapse to `/`; both ingresses route `/<domain>`; a Traefik redirect maps bare `/` → `/overview`. No dynamic base, no per-project builds.

**Tech Stack:** SvelteKit 2 + Svelte 5 (SSR, `svelte-adapter-bun`), Bun + Turborepo, `@rask/ui` (Bits UI), Traefik (k3s ingress), Helm. Repos: `/home/morgan/rask` (branch `feat/project-first-urls`) and `/home/morgan/rask-operator` (branch `feat/project-first-urls`).

## Global Constraints

- **Option 1 only:** host carries the project; path carries the domain. No project segment in the path; no dynamic/runtime base; no per-project image builds. (Spec decisions 1, 2, 4.)
- **No `/default` left in `components/frontends` or `packages/ui/src`.** A grep gate enforces this. (Spec risk.)
- **Static base preserved** so shared images keep working; the dev turbo proxy stays distinct by `/<domain>`. (Spec decision 2.)
- **Bare host → `/overview` via Traefik redirect** scoped to exactly `/` so it never shadows `/api` or `/<domain>`. (Spec decisions 3 + risk.)
- **Follow `rask-frontend` conventions**; `make check` (svelte-check + ty + lint) must pass. **JS uses Bun**, Python uses uv.
- **Commits:** conventional, **no `Co-Authored-By: Claude` trailer**.

## File Structure

- `packages/ui/src/lib/shell/nav-config.ts` — `navMain()` builds `/<domain>` hrefs (drop the project arg/prefix).
- `packages/ui/src/lib/shell/nav-main.svelte` — derive nothing from a project segment; domain is the first path segment; cross-zone compares segment[0].
- `components/frontends/{overview,compute,discover,storage,train,studio}/svelte.config.js` — base `/<domain>`.
- the six apps' `+layout.ts`, `+layout.svelte`, `+page.svelte`, `discover/+page.ts` — comment literals `/default/<domain>` → `/<domain>`.
- `components/frontends/home/microfrontends.json` — dev zone paths `/<domain>`.
- `components/frontends/home/src/lib/components/top-nav.svelte` — link `/overview`.
- `rask-operator/charts/project/templates/ingress.yaml` — routes `/<domain>` + bare-`/` redirect middleware.
- `rask/chart/templates/ingress.yaml` — mirror `/<domain>` (gated single-tenant).

---

### Task 1: Shared nav — drop the project path-segment (`@rask/ui`)

**Files:**
- Modify: `packages/ui/src/lib/shell/nav-config.ts`
- Modify: `packages/ui/src/lib/shell/nav-main.svelte`

**Interfaces:**
- Produces: `navMain(): NavItem[]` (no args) whose hrefs/`match` are `/<domain>` (e.g. `/overview`, `/compute/cluster`). `nav-main.svelte` takes the same `pathname` prop and computes the active domain from path segment[0].

- [ ] **Step 1: Change `navMain` to project-less `/<domain>` hrefs**

In `packages/ui/src/lib/shell/nav-config.ts`:
- Line 9 doc comment: change `(e.g. /default/compute/cluster)` to `(e.g. /compute/cluster)`.
- Replace the function signature + base:
```ts
export function navMain(project: string): NavItem[] {
	const b = `/${project}`;
```
with:
```ts
export function navMain(): NavItem[] {
	const b = '';
```
(Every `${b}/overview` etc. now renders `/overview` — `b` is empty.) Also update the function's doc block (lines ~41-49) to say the sidebar renders inside a project selected by **host**, and hrefs are domain-relative (`/<domain>`), not project-prefixed.

- [ ] **Step 2: Update `nav-main.svelte` to a host-based (project-less) model**

In `packages/ui/src/lib/shell/nav-main.svelte`, replace lines 15-26:
```ts
	// Project-first IA: the active project is the URL's first segment, so the nav's
	// hrefs are prefixed with it (the sidebar only ever renders inside a project).
	const project = $derived(pathname.split('/').filter(Boolean)[0] ?? 'default');
	const items = $derived(navMain(project));

	// MFE zones split by DOMAIN (the segment after the project): /<project>/compute is a
	// different SvelteKit app/zone than /<project>/discover. A sidebar link whose domain
	// differs from the current one leaves THIS app's route manifest, so it must hard-nav
	// (data-sveltekit-reload) — a soft client nav would target a route this app doesn't know.
	// Same-domain links (a domain's own sub-routes) stay soft for SPA speed.
	const currentDomain = $derived(pathname.split('/').filter(Boolean)[1] ?? '');
	const crossZone = (href: string) => (href.split('/').filter(Boolean)[1] ?? '') !== currentDomain;
```
with:
```ts
	// Project-first IA via HOST: the project is the request host (e.g. demo.localhost),
	// so the path carries only the domain. The sidebar's hrefs are domain-relative
	// (/<domain>) and render inside any project.
	const items = $derived(navMain());

	// MFE zones split by DOMAIN (the FIRST path segment now): /compute is a different
	// SvelteKit app/zone than /discover. A sidebar link whose domain differs from the
	// current one leaves THIS app's route manifest, so it must hard-nav
	// (data-sveltekit-reload); same-domain links stay soft for SPA speed.
	const currentDomain = $derived(pathname.split('/').filter(Boolean)[0] ?? '');
	const crossZone = (href: string) => (href.split('/').filter(Boolean)[0] ?? '') !== currentDomain;
```

- [ ] **Step 3: Build `@rask/ui` and verify no `/default` remains in its source**

Run:
```bash
cd /home/morgan/rask
bun --filter @rask/ui run build 2>&1 | tail -5
grep -rn "/default" packages/ui/src && echo "STILL HAS /default" || echo "ok: no /default in @rask/ui src"
```
Expected: build succeeds; grep finds nothing.

- [ ] **Step 4: Commit**

```bash
cd /home/morgan/rask
git add packages/ui/src/lib/shell/nav-config.ts packages/ui/src/lib/shell/nav-main.svelte packages/ui/dist
git commit -m "feat(ui): host-based nav — domain-relative hrefs, drop project path-segment"
```

---

### Task 2: MFE base `/<domain>` + collapse all `/default/` literals (six apps + home)

**Files:**
- Modify: `components/frontends/{overview,compute,discover,storage,train,studio}/svelte.config.js`
- Modify: those apps' `+layout.ts` / `+layout.svelte` / `+page.svelte` / `discover/+page.ts` (comment literals)
- Modify: `components/frontends/home/microfrontends.json`
- Modify: `components/frontends/home/src/lib/components/top-nav.svelte`

**Interfaces:**
- Consumes: Task 1's `navMain()`.
- Produces: each domain app served at base `/<domain>`; dev proxy zones at `/<domain>`; home picker/top-nav linking to `/overview`. No `/default` anywhere in `components/frontends`.

- [ ] **Step 1: Collapse every `/default/` literal to `/`**

The change is uniform — every occurrence is `/default/<domain>` and becomes `/<domain>` (drop the `default/` segment). This covers the six `svelte.config.js` bases, the `microfrontends.json` zone paths, `top-nav.svelte`'s link, and the descriptive comments in the apps' layout/page files. Run:
```bash
cd /home/morgan/rask
grep -rl "/default/" components/frontends --include=*.js --include=*.ts --include=*.svelte --include=*.json | grep -v node_modules | grep -v ".svelte-kit" \
  | xargs sed -i 's#/default/#/#g'
```

- [ ] **Step 2: Verify the bases + zones + link are correct and nothing `/default` remains**

Run:
```bash
cd /home/morgan/rask
echo "--- bases (expect base: '/<domain>') ---"
grep -rn "paths: { base:" components/frontends/*/svelte.config.js
echo "--- microfrontends zones ---"
grep -n '"paths"' components/frontends/home/microfrontends.json
echo "--- top-nav link ---"
grep -n 'href="/overview"' components/frontends/home/src/lib/components/top-nav.svelte
echo "--- residual /default ---"
grep -rn "/default" components/frontends --include=*.js --include=*.ts --include=*.svelte --include=*.json | grep -v node_modules | grep -v ".svelte-kit" && echo "STILL HAS /default" || echo "ok: none"
```
Expected: each base is `'/overview'`, `'/compute'`, `'/discover'`, `'/storage'`, `'/train'`, `'/studio'`; zone paths are `/<domain>` + `/<domain>/:path*`; top-nav link is `/overview`; no residual `/default`.

- [ ] **Step 3: Frontend gate — typecheck/lint/build the workspace**

Run:
```bash
cd /home/morgan/rask
make check 2>&1 | tail -25
```
Expected: svelte-check + ty + lint pass across the six apps + home + `@rask/ui` (the nav-config signature change has no remaining `navMain('default')` callers — Task 1 removed the arg; this gate catches any missed caller).

- [ ] **Step 4: Commit**

```bash
cd /home/morgan/rask
git add components/frontends
git commit -m "feat(frontends): base /<domain> + drop /default literals (host-based project URLs)"
```

---

### Task 3: Ingress routes `/<domain>` + bare-host redirect (both charts)

**Files:**
- Modify: `rask-operator/charts/project/templates/ingress.yaml`
- Modify: `rask/chart/templates/ingress.yaml`

**Interfaces:**
- Consumes: per-project frontends served at base `/<domain>` (Task 2).
- Produces: the per-project Ingress routes `/<domain>` → `<proj>-<domain>:3000`, plus a Traefik `Middleware` `<proj>-root-redirect` and a catch-all `/` route that redirects bare host → `/overview`.

- [ ] **Step 1: Operator ingress — route `/<domain>` and add the redirect**

In `rask-operator/charts/project/templates/ingress.yaml`, change the frontend path from `/default/{{ $domain }}` to `/{{ $domain }}`:
```yaml
          {{- range $domain := $fe.apps }}
          - path: /{{ $domain }}
            pathType: Prefix
            backend:
              service:
                name: {{ $p }}-{{ $domain }}
                port:
                  number: {{ $fe.port }}
          {{- end }}
```
Then, at the top of the file (before the `Ingress`), add a Traefik redirect Middleware and reference it from the Ingress; and add a catch-all `/` path (lowest precedence) that the middleware intercepts. Add this Middleware document at the end of the file:
```yaml
---
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: {{ $p }}-root-redirect
  namespace: {{ .Release.Namespace }}
  labels:
    app.kubernetes.io/managed-by: rask-operator
    platform.rask.io/project: {{ $p }}
spec:
  redirectRegex:
    # Only a bare host ("" or "/") redirects to /overview. Deeper paths
    # (/api, /<domain>) are matched by their own higher-precedence rules.
    regex: "^(https?://[^/]+)/?$"
    replacement: "${1}/overview"
    permanent: false
```
Add the middleware annotation to the Ingress `metadata` (Traefik reads `<namespace>-<name>@kubernetescrd`):
```yaml
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: {{ .Release.Namespace }}-{{ $p }}-root-redirect@kubernetescrd
```
and add a catch-all `/` path as the LAST rule under `paths:` (after the `/<domain>` loop) so a bare-host request has a router for the middleware to act on:
```yaml
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ $p }}-overview
                port:
                  number: {{ $fe.port }}
```
(The middleware redirects bare host to `/overview` before the backend serves; non-bare unmatched paths fall through to overview, which 404s within the app — acceptable.)

- [ ] **Step 2: rask single-tenant ingress — mirror `/<domain>`**

In `rask/chart/templates/ingress.yaml`, the domain-route loop currently emits `/default/<domain>` gated on `singleTenant`. Change its path from `/default/{{ ... }}` to `/{{ ... }}` (keep the existing `singleTenant` guard and the `/api` + catch-all `/` rules untouched). (This path is gated off by default; the edit keeps it consistent if re-enabled.)

- [ ] **Step 3: Verify both charts render**

Run:
```bash
cd /home/morgan/rask-operator
helm template demo charts/project --set projectName=demo > /tmp/proj.yaml 2>&1 && echo "operator render OK"
grep -E "path: /overview|path: /storage|path: /$|kind: Middleware|root-redirect|router.middlewares" /tmp/proj.yaml
grep -q "/default/" /tmp/proj.yaml && echo "STILL /default in operator ingress" || echo "ok: no /default"
cd /home/morgan/rask
helm template rask ./chart --set singleTenant.enabled=true > /tmp/st.yaml 2>&1 && echo "rask render OK"
grep -q "path: /default/" /tmp/st.yaml && echo "STILL /default in rask ingress" || echo "ok: no /default (single-tenant)"
```
Expected: operator renders `/overview`…`/studio` paths, a `/` catch-all, the `Middleware`, and the router.middlewares annotation; no `/default` in either chart.

- [ ] **Step 4: Run the operator chart render test (Go)**

```bash
cd /home/morgan/rask-operator
go test ./internal/helm/ 2>&1 | tail -3
```
Expected: PASS. (If `chart_test.go` asserts a `/default/...` ingress path, update that assertion to `/overview` to match — keep the test green.)

- [ ] **Step 5: Commit (both repos)**

```bash
cd /home/morgan/rask-operator
git checkout -b feat/project-first-urls 2>/dev/null || git checkout feat/project-first-urls
git add charts/project/templates/ingress.yaml internal/helm/chart_test.go
git commit -m "feat(chart): route /<domain> + bare-host->/overview redirect (project-first URLs)"
cd /home/morgan/rask
git add chart/templates/ingress.yaml
git commit -m "feat(chart): single-tenant ingress /<domain> (project-first URLs)"
```

---

### Task 4: Live build + deploy + browser e2e (controller-run, checkpointed)

**Files:** none (build, deploy, verify). Run directly in-session; stop and report at each checkpoint.

- [ ] **Step 1: Rebuild the six MFE images (base is compiled in) + import to k3s**

```bash
cd /home/morgan/rask
make k3s-build 2>&1 | tail -8     # builds the frontend images (and others) with the new base
make k3s-import 2>&1 | tail -5    # imports into k3s containerd
```
(If `make k3s-build` rebuilds everything, that's fine; the MFE images are what matter here. Confirm the six `*:dev` frontend images were rebuilt.)

- [ ] **Step 2: Rebuild + import the operator (embedded chart changed) and roll**

```bash
cd /home/morgan/rask-operator
docker build -t rask-operator:dev . 2>&1 | tail -3
docker save rask-operator:dev | sudo -n k3s ctr images import - 2>&1 | tail -1
kubectl -n rask-operator-system rollout restart deploy/rask-operator-controller-manager
kubectl -n rask-operator-system rollout status deploy/rask-operator-controller-manager --timeout=120s
```

- [ ] **Step 3: Re-reconcile a project and roll its frontends to the new images**

```bash
kubectl annotate project demo2 rask.io/rev="urls-$(date +%s)" --overwrite
# frontends use :dev (same tag) — force them to pull the rebuilt image:
for d in overview compute discover storage train studio; do
  kubectl -n project-demo2 rollout restart deploy/demo2-$d 2>/dev/null
done
for d in overview storage; do kubectl -n project-demo2 rollout status deploy/demo2-$d --timeout=120s 2>&1 | tail -1; done
```

- [ ] **Step 4: Verify routes + redirect (HTTP)**

```bash
NODE_IP=10.16.51.56
echo "--- new paths 200 ---"
curl -s -L --resolve demo2.localhost:80:$NODE_IP -o /dev/null -w "/overview -> %{http_code}\n" http://demo2.localhost/overview
curl -s -L --resolve demo2.localhost:80:$NODE_IP -o /dev/null -w "/storage  -> %{http_code}\n" http://demo2.localhost/storage
echo "--- bare host redirects to /overview ---"
curl -s --resolve demo2.localhost:80:$NODE_IP -o /dev/null -w "bare / -> %{http_code} -> %{redirect_url}\n" http://demo2.localhost/
echo "--- old /default path gone ---"
curl -s --resolve demo2.localhost:80:$NODE_IP -o /dev/null -w "/default/overview -> %{http_code}\n" http://demo2.localhost/default/overview
```
Expected: `/overview` and `/storage` → 200; bare `/` → 302/308 with `redirect_url` ending `/overview`; `/default/overview` → 404 (route gone).

- [ ] **Step 5: Browser-verify (not just HTTP)**

Using the established Playwright flow with `--resolve demo2.localhost:80:$NODE_IP` reachable:
- `http://demo2.localhost/` → lands on the overview page (after redirect).
- Sidebar nav: click Storage → URL becomes `/storage`, page renders; click Compute → `/compute`; Overview → `/overview`. No `/default` in any URL.
- Repeat a spot-check on a second project host if one is provisioned.
Capture screenshots as evidence.

- [ ] **Step 6: Record the result in the ledger**

Append the outcome (new `/<domain>` URLs work, bare-host redirect works, `/default` gone, nav soft/hard navigation intact) to `.superpowers/sdd/progress.md`.

---

## Self-Review

**Spec coverage:**
- Component 1 (MFE base `/<domain>`) → Task 2 Step 1-2. ✓
- Component 2 (`@rask/ui` nav drops project segment) → Task 1. ✓
- Component 3 (home dev proxy + picker links) → Task 2 (microfrontends.json + top-nav). ✓
- Component 4 (both ingresses route `/<domain>`) → Task 3 Steps 1-2. ✓
- Component 5 (bare-host redirect) → Task 3 Step 1 (Middleware + `/` route). ✓
- Testing (make check; no `/default`; dev proxy; live browser; bare-host redirect) → Task 1 Step 3, Task 2 Steps 2-3, Task 4. ✓
- Risks (image rebuild; stale literals; redirect scoped to `/`) → Task 4 Step 1-2 (rebuild), grep gates (Task 1/2), Task 3 Step 1 regex `^(https?://[^/]+)/?$`. ✓

**Placeholder scan:** No TBD/TODO. Logic edits show exact before/after; mechanical literal change is one exact `sed` with verification greps; ingress/middleware shown in full. Task 3 Step 4 conditionally updates a test assertion only if present (concrete: change to `/overview`).

**Consistency:** `navMain()` (no args) defined in Task 1 and used by `nav-main.svelte` in the same task; bases `/<domain>` (Task 2) match ingress paths `/<domain>` (Task 3) and the dev zones (Task 2); `currentDomain` reads segment[0] consistent with the new `/<domain>` shape; the redirect target `/overview` matches the overview base. No project segment anywhere post-change.
