# Audit: four-zone micro-frontend composition vs the `micro-frontends` skill

Repo: `/home/blackwell/Desktop/lance-ns` — branch `main` (clean at `e489f2b`).
Skill read in full: `SKILL.md` + all five references
(`composition.md`, `routing-and-orchestration.md`, `principles.md`, `communication.md`,
`module-federation.md` — the last is N/A, no bundler federation here).

Read-only audit. Every claim below cites `path:line` and a command that was actually run.

## Executive summary

1. The **declaration chain and the zone contract are excellent** — `microfrontends.json` ↔ `paths.base`
   ↔ `chart/values.yaml` ↔ rendered Ingress ↔ vite ports all agree and are machine-checked by 278 green
   gate tests; `turbo boundaries` is clean; no zone imports another zone's `src/`; per-zone
   image/Deployment/Service/tag/turbo-filter independence is demonstrable, not asserted.
2. **BUG (high): four cross-zone links hard-navigate into a 404.** `/data`, `/data/projects/<p>` and
   `/lineage` are pre-merge paths no zone serves — proven 404 against the built `home` zone. One of them
   is in the shared shell (`project-switcher.svelte:55`, every page of 3 of 4 zones); another is the
   home landing page's project grid, i.e. the product's front door.
3. **BUG (high): the only composed-page verification in the repo cannot run.**
   `scripts/verify_cross_zone_oidc.sh:33` gates readiness on `curl /data` expecting 200/308; it gets 404,
   so the script dies at its own timeout before driving anything.
4. **BUG (medium): a same-zone link forces a document reload and a test pins it.**
   `AuditViewer.svelte:341` hard-navigates `${base}/data/...`; `e2e/admin/audit.spec.ts:103` asserts the
   reload attribute, directly contradicting `e2e/admin/tenants.spec.ts:72` for the same navigation class.
5. **Root cause of all three: nothing ever composes the four zones.** CI runs per-zone Playwright at
   per-zone origins and sets `frontend.enabled=false` on the live stack; no CI leg builds a zone image.
   Every gate is a good *static* gate, and the bug class here — a link that type-checks, lints, renders,
   and 404s — is exactly what only a composed gate catches.

---

## Scope map (established first, so later findings can be located)

Four zones, all SvelteKit + `svelte-adapter-bun`:

| Zone | dir | `paths.base` | dev port | role |
|---|---|---|---|---|
| home | `frontend/components/frontends/home` | *(none)* | 5273 | default app / catch-all `/` |
| lakehouse | `frontend/components/frontends/lakehouse` | `/lakehouse` | 5174 | catalog+lineage+models+admin |
| media | `frontend/components/frontends/media` | `/media` | 5173 | media/corpus |
| annotator | `frontend/components/frontends/annotator` | `/annotator` | 5177 | labeling canvas |

Shared packages: `frontend/packages/{api,config,engine,labeling,media-api,ui,zone-contract}`.

Composition model as declared: **routing-based zones** (skill
`composition.md:5`, `routing-and-orchestration.md:33-47`) — the coarsest independent-deploy
model, page-level boundaries, no shared runtime.

---

*(findings appended below as they are established)*
## 1. Zone boundaries — does each zone own a coherent slice?

### 1.1 No zone imports another zone's `src/` — CONFORMS

```
$ cd frontend/components/frontends && grep -rn --include=*.ts --include=*.svelte --include=*.js --include=*.mjs \
    -E "['\"][^'\"]*(home|lakehouse|media|annotator)/src/" . | grep -v node_modules | grep -v '\.svelte-kit'
(no matches outside build/ output)

$ for z in home lakehouse media annotator; do grep -rnE "from ['\"](home|lakehouse|media|annotator)['\"]" $z/src $z/e2e; done
(empty)
```

The only `../../..`-escaping imports are inside generated `build/server/index.js`
(e.g. `media/build/server/index.js:3004`), which is SvelteKit's own vendored runtime, not zone code.

Verdict: **CONFORMS** — `principles.md:46` ("Isolate team code") holds at the import level.

### 1.2 Slices are vertical, not horizontal — CONFORMS

Each zone ships its own BFF endpoints rather than sharing a gateway route:
`lakehouse/src/routes/capi/**` (41 `+server.ts`) + `lakehouse/src/routes/api/**` (8),
`media/src/routes/api/**` (8), `annotator/src/routes/api/**` (6),
`home/src/routes/capi/v1/projects/+server.ts` — counted with
`find <zone>/src/routes/<dir> -name '+server.ts' | wc -l`.
`chart/templates/ingress.yaml:56-60` records the deliberate removal of the root-absolute
`/api → media-backend` side door precisely so each zone owns its own vertical:

> "the media/annotator zones are full BFF zones now — their clients fetch `/<zone>/api/*`, served by
> the zone's own SvelteKit routes … Removing the side-door also stops unauthenticated edge traffic
> reaching the media services directly."

That is `principles.md:20` ("Be team-first", vertical cut UI→API) satisfied with a reason in the code.

### 1.3 `lakehouse` is one zone spanning four areas — DEVIATES-WITH-REASON

`chart/values.yaml:563-566` states the reason explicitly:

> "catalog + lineage + models + admin were four separate zones on four Deployments, all reading the
> same catalog/lineage planes and all shipped from this one shared image tag — so they paid for four
> SSR servers and a full document reload between them while collecting none of the independent-deploy
> payoff."

This is the skill's own advice (`principles.md:112` "Start with a monolith", `principles.md:25`
"if a typical feature touches more than one slice, your boundaries are wrong"). Documented reason,
in-code. Verdict: **DEVIATES-WITH-REASON**.

Caveat worth recording: with one owner across all four zones, the skill's own "When NOT to use
micro-frontends" (`SKILL.md`, *"A single team owns the whole frontend"*) applies to the remaining
split too. The zones are not there for team autonomy — they are there for stack isolation
(`annotator` ships pixi.js + `@repo/engine`; `media` ships embedding-atlas/elkjs/webgpu; neither is
loaded when you are on `/lakehouse`). That is a legitimate but *different* justification than the
skill's, and it is not written down anywhere in the repo.

---

## 2. Composition model — routing-based zones on one origin

### 2.1 The mechanism is real and is a full document load — CONFORMS

- Declared model: `frontend/components/frontends/home/microfrontends.json:1` uses
  `https://turborepo.dev/microfrontends/schema.json` with `applications` + `routing.paths` — exactly
  the zones shape in `routing-and-orchestration.md:37-45`.
- Enforced by an AST gate, not a convention:
  `frontend/packages/zone-contract/src/cross-zone-reload.ts:83` `findViolations()` parses each
  `.svelte` with **Svelte's own compiler** (`cross-zone-reload.ts:17`) and flags any `<a>` whose href
  matches `^/(lakehouse|media|annotator)(/|$)` (`cross-zone-reload.ts:20-23`) that lacks
  `data-sveltekit-reload` (`cross-zone-reload.ts:102`).
- The gate correctly treats `data-sveltekit-reload="off"` as a violation
  (`cross-zone-reload.ts:67-71`, test at `cross-zone-reload.test.ts:61-62`).
- Cross-zone links carry it in practice: `packages/ui/src/lib/shell/top-navbar.svelte:152`,
  `:194`, `:223`, `:239`, `:255` all render
  `data-sveltekit-reload={crossZone(item.href) ? '' : undefined}`;
  `packages/ui/src/lib/shell/zone-nav.svelte:25` does the same per-leaf via `leaf.reload`.
- Cross-zone navigation is *softened* the way the skill prescribes
  (`routing-and-orchestration.md:68-70`): `packages/ui/src/lib/shell/nav-config.ts:278-285`
  `prefetchDocument()` injects `<link rel="prefetch">` on hover/focus, with an honest scope comment
  ("Chromium and Firefox honor it; Safari does not").
- Auth is the shell's, shared across zones via one sealed cookie on one origin
  (`chart/values.yaml:574-577`: "one origin, one sealed cookie, so a login on any zone is a login on
  every zone") — `communication.md:135` ("the shell owns the token").

Verdict: **CONFORMS**. This is a genuinely better-than-textbook implementation of the zone contract:
the gate runs on the framework's own parser, and it knows that `{base}/…` hrefs are same-zone
(`cross-zone-reload.ts:25-26,44-52` renders `{…}` as an opaque non-`/` sentinel).

### 2.2 BUG — four cross-zone links point at paths **no zone owns** (hard-nav straight into a 404)

The gate in 2.1 only checks *whether* a zone-path link reloads. Nothing checks that an absolute href
resolves to a path some zone actually serves. Post-merge, `/data`, `/lineage`, `/models`, `/admin` are
**areas inside `/lakehouse`**, not origin-level prefixes — and four links still use the pre-merge form.

Ownership, proven from the built artifacts and the chart:

```
$ grep -oE '\bid: "[^"]*"' frontend/components/frontends/home/build/server/manifest.js | sort -u
id: "/"
id: "/auth/callback"
id: "/auth/login"
id: "/auth/logout"
id: "/capi/v1/projects"
```
`home` serves five routes. Nothing under `/data` or `/lineage`.

`chart/values.yaml:562-573` → the only Ingress prefixes are `/lakehouse`, `/media`, `/annotator`,
plus `/` → home (`chart/templates/ingress.yaml:31-41` ranges `frontend.apps` for the non-catchAll
paths, `:62-72` emits `/` for the catchAll). `/data` therefore falls through to `home`, which 404s.

The four sites (found with
`grep -rnE 'href=(\{`|")(/data|/lineage|/models|/admin)([^a-z-]|$)' --include=*.svelte components packages`):

| # | Site | href as written | resolves to |
|---|---|---|---|
| a | `frontend/packages/ui/src/lib/shell/project-switcher.svelte:55` | `href="/data" data-sveltekit-reload` | `home` → 404 |
| b | `frontend/components/frontends/home/src/routes/+page.svelte:25` | `href={`/data/projects/${p.project}`} data-sveltekit-reload` | `home` → 404 |
| c | `frontend/components/frontends/lakehouse/src/lib/data/TableDetail.svelte:930` and `:938` | `href="/lineage" data-sveltekit-reload` | `home` → 404 |
| d | `frontend/components/frontends/lakehouse/src/lib/models/PipelineControl.svelte:86` | `href="/lineage" data-sveltekit-reload` | `home` → 404 |

Blast radius, worst first:

- **(a) is in the shared shell and renders on three of four zones.**
  `frontend/packages/ui/src/lib/shell/app-shell.svelte:107` renders `<ProjectSwitcher …>`, and
  `AppShell` is imported by `home/src/routes/+layout.svelte:7`,
  `lakehouse/src/routes/+layout.svelte:8`, `media/src/routes/+layout.svelte:9`
  (`grep -rn "AppShell" --include=*.svelte components/frontends/*/src`). The project switcher is the
  head of the global chrome (`nav-config.ts:214`: "the project switcher sits at the head of the bar
  on every zone"), so the single most persistent control in the estate is a 404 on every page of
  three zones.
- **(b) is the product's front door.** `home/src/routes/+page.svelte:21-46` is the landing page's
  project grid — the first click a signed-in user makes lands on a 404.

Correct form is `/lakehouse/data…` / `/lakehouse/lineage`, which is what the nav config already uses
(`frontend/packages/ui/src/lib/shell/nav-config.ts:92,95,98,103,111,115,119,123,131`) and what the
e2e suites assert (`lakehouse/e2e/admin/tenants.spec.ts:66`, `:69` expect
`/lakehouse/admin/audit?…` and `/lakehouse/data/warehouses`).

Verdict: **BUG**. This is the failure mode the skill names in `principles.md:136-142`
("Integration-test the composed page … broken cross-slice navigation — every failure mode that
exists *because* of integration") and `routing-and-orchestration.md:57` ("Never route to a path the
target can't serve").

Why every existing gate misses it — the gate's own test *pins* the blind spot:

```
frontend/packages/zone-contract/src/cross-zone-reload.test.ts:25-28
	expect(isCrossZonePath('/data/tables')).toBe(false);
	expect(isCrossZonePath('/lineage')).toBe(false);
	expect(isCrossZonePath('/models/experiments')).toBe(false);
	expect(isCrossZonePath('/admin/dlq')).toBe(false);
```

That assertion is *correct* for its purpose (those are same-zone area paths that must stay soft) —
but it means the four pre-merge literals are invisible to the one gate that reads every `<a>`. The
missing gate is the complementary one: **every absolute `<a href>` must start with a prefix some zone
serves** (`/lakehouse`, `/media`, `/annotator`, or one of home's five routes). All the machinery
already exists — `zoneDirs()`, `svelteBase()`, `chartApps()` in
`frontend/packages/zone-contract/src/manifest.ts:42,70,102` plus `findViolations`' href flattener.

### 2.3 BUG — one same-zone link forces a document reload, and an e2e test pins the wrong contract

`frontend/components/frontends/lakehouse/src/lib/admin/AuditViewer.svelte:170-177` builds the jump
target with `${base}` (so `/lakehouse/data/tables/…` — *same zone*), then hard-navigates it:

```
AuditViewer.svelte:340-341
	<!-- Cross-zone jump: leaves this zone's route manifest, so hard-navigate. -->
	<a class="btn jumplink" href={resourceHref(drawerEvent.resource)} data-sveltekit-reload>
```

The comment is stale post-merge: `/lakehouse/data/tables/[table]` *is* in this zone's manifest
(`grep -oE '\bid: "/data[^"]*"' lakehouse/build/server/manifest.js` → `id: "/data/tables/[table]"`).
The sibling panel gets it right, with the reason written down:

```
frontend/components/frontends/lakehouse/e2e/admin/tenants.spec.ts:70-72
	// The warehouse admin page is in the catalog AREA of this same zone now, so this jump is a soft
	// navigation — forcing a document reload on it would discard the merge's payoff.
	await expect(jump).not.toHaveAttribute('data-sveltekit-reload', '');
```

…while the audit suite asserts the opposite for an equivalent link:

```
frontend/components/frontends/lakehouse/e2e/admin/audit.spec.ts:100-103
	// …a cross-zone jump link to the resource page (hard nav)…
	await expect(jump).toHaveAttribute('href', '/lakehouse/data/tables/db1%24t');
	await expect(jump).toHaveAttribute('data-sveltekit-reload', '');
```

Two tests in the same directory encode contradictory contracts for the same navigation class, and the
audit one is green because it pins the defect. Verdict: **BUG** (`cross-zone-reload.ts:13-15`:
"a link between those is same-zone and must NOT hard-navigate, or every area hop pays a document
load again"). Impact is a wasted full-document load, not a 404 — lower severity than 2.2.

### 2.4 Everything else routes correctly — CONFORMS

Full enumeration of href forms (`grep -rhoE "href=[^ >]*" --include=*.svelte components/frontends/*/src packages/*/src | sort | uniq -c`)
returns **53** distinct forms (`… | sort -u | wc -l` → 53). Apart from the four in 2.2, every one is either `{base}`-prefixed
(same-zone, correctly soft) or a genuine cross-zone/home path with `data-sveltekit-reload`:

- `loginHref` (23 uses) = `` `/auth/login?redirect=…` `` (`packages/ui/src/lib/shell/navbar-user.svelte:34`)
  → home zone's `auth/login/+server.ts`, correctly reloaded.
- The one cross-zone sidebar leaf declares itself: `components/frontends/media/src/lib/nav.ts:18-24`
  `{ title: 'Annotate', href: '/annotator', …, reload: true }`, and `zone-nav.svelte:25` honors it.
- Per-zone nav configs all use full zone-prefixed paths
  (`lakehouse/src/lib/{data,models,admin,lineage}/nav.ts`, `media/src/lib/nav.ts`).

---

## 3. The declaration chain

Five declarations of the same fact, and they agree:

| Zone | `microfrontends.json` | `svelte.config.js` `paths.base` | `chart/values.yaml frontend.apps` | rendered Ingress path | vite dev port |
|---|---|---|---|---|---|
| home | *(no `routing`)*, port 5273 (`:4-10`) | *(absent — comment at `home/svelte.config.js:10`: "The DEFAULT app (home) owns '/', so no base path.")* | `{ name: home, catchAll: true }` (`values.yaml:562`) | `/` | 5273 (`vite.config.ts:15`, `strictPort: true`) |
| lakehouse | `["/lakehouse", "/lakehouse/:path*"]` (`:17-23`) | `'/lakehouse'` (`lakehouse/svelte.config.js:12`) | `{ name: lakehouse, path: /lakehouse }` (`values.yaml:567`) | `/lakehouse` | 5174 (`vite.config.ts:15`) |
| media | `["/media", "/media/:path*"]` (`:30-36`) | `'/media'` (`media/svelte.config.js:24`) | `{ name: media, path: /media }` (`values.yaml:572`) | `/media` | 5173 (`vite.config.ts:18`) |
| annotator | `["/annotator", "/annotator/:path*"]` (`:43-49`) | `'/annotator'` (`annotator/svelte.config.js:20`) | `{ name: annotator, path: /annotator }` (`values.yaml:573`) | `/annotator` | 5177 (`vite.config.ts:12`) |

Ingress column proven by rendering, not by reading the template:

```
$ helm template lance chart --set ingress.enabled=true | awk '/kind: Ingress/,/^---/'
          - path: /lakehouse   → service lance-web-lakehouse:3000
          - path: /media       → service lance-web-media:3000
          - path: /annotator   → service lance-web-annotator:3000
          - path: /            → service lance-web-home:3000
```

Longest-prefix-first ordering holds (`chart/templates/ingress.yaml:29-30` emits the domain zones
before the catch-all; `routing-and-orchestration.md:16` requires exactly this).

The chain is also *machine-checked*, which is stronger than any audit:
`frontend/packages/zone-contract/src/manifest.test.ts:66-81` asserts `svelteBase(zone) === '/'+zone`
**and** the `microfrontends.json` paths **and** the chart `path` for every zone found on disk, and
`:50-56` asserts no declaration names a zone that has no directory (both directions). The dev
composition proxy reads the *same* file rather than a copy
(`frontend/packages/zone-contract/src/proxy.ts:19,31` `routingConfig()`), so there is no second source
of truth for the local harness either — `principles.md:141`'s "local-dev story" requirement, met with
a real proxy (`proxy.ts:60-142`, including WebSocket/HMR bridging with the reason recorded at `:70-74`).

```
$ cd frontend && bunx vitest run --root packages/zone-contract
 Test Files  5 passed (5)
      Tests  278 passed (278)
```

Verdict for the chain itself: **CONFORMS** — and unusually well. This is the strongest part of the
implementation.

### 3.1 Two stale comments still name the seven pre-merge zones — DEVIATES (documentation, not behaviour)

- `chart/templates/ingress.yaml:5`: *"Each domain zone owns its base path (/data,/lineage,/models,/admin
  → that zone's Service)"*. The rendered output (above) has no such paths. Same class as the bug
  fixed in `e489f2b` ("ZONES still listed the seven pre-merge zones").
- `chart/templates/_helpers.tpl:27`: *`Call: include "lance.frontendImage" (list $root "data")`* — `data`
  has not been a zone since the merge.

Both are inside files that `manifest.test.ts:283-293` *does* scan, but the `DEAD` list
(`manifest.test.ts:282`) is `['@rask/', '@lance/', 'packages/rask-ui', 'components/frontends/data']` —
none of which matches a bare `/data` path or `"data"` zone name. Verdict: **DEVIATES** (no reason
given anywhere; it is drift). Low severity — no runtime effect — but it is the same comment that
would mislead the next person fixing 2.2.

---

## 4. Shared-package layering

### 4.1 `turbo boundaries` — clean

```
$ cd frontend && bunx turbo boundaries
• turbo 2.10.6
Checking packages...

Checked 2925 files in 11 packages, no issues found
```

(Verbatim, exit 0.)

### 4.2 No cycles; the internal graph is a depth-2 DAG — CONFORMS

Declared (`package.json` `dependencies`+`devDependencies`, read with a python one-liner over
`packages/*/package.json`):

```
@repo/api           -> []
@repo/config        -> []
@repo/ui            -> []
@repo/media-api     -> [@repo/config]
@repo/engine        -> [@repo/config]
@repo/labeling      -> [@repo/config, @repo/media-api]
@repo/zone-contract -> [@repo/config]
```

Actual source imports (`grep -rhoE "from '@repo/[a-z-]+" packages/<p>/src`) — only one edge exists at
all: `@repo/labeling → @repo/media-api`. `@repo/config` is consumed as a *file* (`tsconfig.base.json`
is its only meaningful export, `packages/config/package.json` `exports`), not as code. So the runtime
graph is a single edge; no cycles possible. **CONFORMS**.

### 4.3 Layering discipline — CONFORMS with one shape worth naming

- `@repo/ui` has **zero** `@repo/*` imports — verified by the grep above. It deliberately does not
  import `@repo/api` even though it renders identity: `packages/ui/src/lib/shell/nav-config.ts:37-46`
  re-declares the `Me` contract *structurally*, with the reason inline: *"mirrored structurally from
  @repo/api (the shared shell never imports app data — same seam as `NavUser`)"*. The canonical
  definition is `packages/api/src/me.ts:20-29` (a valibot schema, `MeSchema`, so it is parsed at the
  boundary — `me.ts:57` `v.parse(MeSchema, await res.json())`).
  This keeps the chrome package free of the data layer. Verdict: **DEVIATES-WITH-REASON**, reason
  cited in-code.
  Gap: nothing pins the two shapes together. `packages/api/tests/me.test.ts:9` types a fixture as
  `Me` from `@repo/api` only; there is no assertion that `nav-config.ts`'s `Me` is structurally
  identical. A field added to `MeSchema` and used by the navbar would need two edits with no gate.
  `principles.md:152` ("Versioned contracts between slices … Keep a small reviewed registry") is
  satisfied in spirit (one frozen shape, documented) but not mechanically.
- All shared packages except `@repo/ui` export **raw TypeScript source**
  (`@repo/api` `exports['.']` → `./src/index.ts`; same for engine/labeling/media-api), bundled into
  each zone by that zone's vite. `@repo/ui` is the only one with a build step (`svelte-package` →
  `dist/`), which is why `.docker/frontend.dockerfile:47-49` pre-builds it before the zone build.
  Consequence, stated honestly: this is **build-time composition for the shared layer**
  (`composition.md:19-32`) sitting under routing composition for the zones. A change to `@repo/api`
  or `@repo/ui` requires rebuilding and redeploying **all four** zones — the exact coupling
  `communication.md:131` warns about ("a shared package is a shared build/runtime dependency, which
  is the one thing the core rule avoids"). The skill permits this for genuinely cross-cutting
  concerns (auth, theme, chrome), which is what `@repo/api` (session/BFF/OIDC) and `@repo/ui`
  (chrome + design system) are. Verdict: **DEVIATES-WITH-REASON**.

### 4.4 Zone-specific logic inside a shared package — DEVIATES-WITH-REASON, one instance is a BUG

`packages/ui/src/lib/shell/nav-config.ts:89-262` hard-codes **22 route rows for other zones**
(`/lakehouse/data/projects`, `/media/atlas`, `/annotator`, …). By `principles.md:105-110` that is
correct — the shell owns top-level routing and the cross-zone jump list, and the file says so
(`nav-config.ts:80-83`: *"Deliberately a SUBSET of the zone's own sidebar (`ZoneNav`): this is the
cross-zone jump list, not a mirror of in-zone navigation"*). The complementary two-level half is
real: each zone passes its OWN sidebar (`nav-config.ts:20-26`; per-zone configs at
`lakehouse/src/lib/{data,models,admin,lineage}/nav.ts`, `media/src/lib/nav.ts`,
`home/src/lib/nav.ts`). So: navbar = flat/shell-owned, sidebar = two-level/zone-owned. That is the
split `routing-and-orchestration.md:106-108` recommends. **DEVIATES-WITH-REASON.**

Residual cost, unrecorded anywhere: the flat half means adding a lakehouse sub-area is a change to a
package all four zones bundle → all four redeploy (`routing-and-orchestration.md:107`: *"every new
sub-route a team adds requires a shell change → couples deploys"*).

The one shared-package item that is not defensible is finding **2.2(a)**:
`packages/ui/src/lib/shell/project-switcher.svelte:55` `href="/data"` — a zone-specific route literal
in the shared shell that no zone serves, rendered on three of four zones.

### 4.5 Duplication worth flagging (not an MFE violation)

`loginHref` is re-derived identically in **23 files**:

```
$ grep -rln "const loginHref" packages/*/src components/frontends/*/src | wc -l
23
```

1 in the shared shell (`packages/ui/src/lib/shell/navbar-user.svelte:34`) and 22 in `lakehouse`
(all of `src/lib/{admin,data,models}/*.svelte` plus 8 `src/routes/**/+page.svelte`), each as
`` `/auth/login?redirect=${encodeURIComponent(page.url.pathname)}` ``. The home-zone login route is a
cross-zone contract; it belongs in one place (`@repo/api` or `@repo/ui/shell`) so the redirect
encoding cannot drift. Verdict: **DEVIATES** (no reason recorded). Cosmetic today.

---

## 2.2 addendum — the 404 is now proven empirically, not inferred

Ran the **actual built home zone** (the catch-all that receives these paths under the Ingress) and
probed it:

```
$ cd frontend/components/frontends/home && PORT=5399 HOST=127.0.0.1 bun ./build/index.js &
$ for p in / /data /data/projects/acme /lineage /auth/login; do
    curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:5399$p"; done
/                    -> 200
/data                -> 404
/data/projects/acme  -> 404
/lineage             -> 404
/auth/login          -> 302
```

Server log (same run) confirms each as an unrouted request, not a redirect:

```
[zone-error] {"zone":"home","side":"server","message":"Not found: /data","status":404,"routeId":null,…}
[zone-error] {"zone":"home","side":"server","message":"Not found: /data/projects/acme","status":404,…}
[zone-error] {"zone":"home","side":"server","message":"Not found: /lineage","status":404,…}
```

Finding 2.2 is therefore **CONFIRMED**, not plausible. (Side benefit: this output also proves
`principles.md:144` "Observe per slice" — every error carries `"zone":"home"`, from
`frontend/components/frontends/home/src/hooks.server.ts` /
`hooks.client.ts` via `makeZoneServerErrorHandler('home')` /
`makeZoneClientErrorHandler('home')`. All four zones do this.)

### 2.5 BUG — `scripts/verify_cross_zone_oidc.sh` cannot pass: its readiness probe is `/data`

The live cross-zone drive is gated on a probe of a path that no longer exists:

```
scripts/verify_cross_zone_oidc.sh:32-36
  w=$(curl -s -o /dev/null -w '%{http_code}' -m2 "http://localhost:$ORIGIN_PORT/data" 2>/dev/null || true)
  d=$(curl … /dex/.well-known/openid-configuration …)
  { [ "$w" = "200" ] || [ "$w" = "308" ]; } && [ "$d" = "200" ] && { echo "✓ forwards ready (ingress/data=$w dex=$d)"; break; }
  [ "$i" = "30" ] && { … fail "port-forwards never became ready"; }
```

`/data` returns 404 (proven above), which is neither 200 nor 308, so the loop exhausts 30 iterations
and the script dies at `:35` — **before** reaching the drive it exists to perform. This is not a
comment: it is the executable gate.

The `.mjs` half it wraps was correctly updated (`scripts/verify_cross_zone_oidc.mjs:49,51,53,66,73,85`
all use `/lakehouse/data`, `/media`, `/lakehouse/models/pipeline`), so the shell wrapper drifted
away from its own payload. Two stale comments in the same file: `:3` "(sign in on /data → still
signed in on /admin)" and `:7` "the 5 zone images built" (there are four).

`frontend/packages/zone-contract/src/manifest.test.ts:288-289` *does* scan this exact file — but only
for `DEAD = ['@rask/', '@lance/', 'packages/rask-ui', 'components/frontends/data']`
(`manifest.test.ts:282`), none of which matches a bare `/data` URL.

Verdict: **BUG**. Severity: this is the only artifact in the repo that verifies the composed page
live, so it being unrunnable means the composition is currently unverified end-to-end.

### 2.6 The composed page is never integration-tested with the zones deployed — DEVIATES

`principles.md:136` is explicit: *"Test slices assembled together … broken cross-slice navigation —
every failure mode that exists because of integration."* What CI actually runs:

- `.github/workflows/ci.yml:80` `dagger call frontend` → `.dagger/frontend.go:52`
  `bunx turbo run check check:tsgo test lint fmt:check` — per-package, no composition.
- `.github/workflows/ci.yml:102` `bunx turbo run test:e2e --concurrency=1` — **per-zone** Playwright
  against **that zone's own vite dev server**, with `/api/**` mocked in-page
  (`.github/workflows/ci.yml:57-58`: "hermetic for CI (playwright.config.ts mocks every /api/** via
  page.route)"). Each zone is tested at its own origin, alone.
- `.github/workflows/ci.yml:143` `e2e-stack` → `make e2e-ci` → `scripts/e2e_stack.sh:110`
  **`--set frontend.enabled=false`** — the live kind stack deliberately does not deploy the zones at
  all.
- No CI job builds the zone images either — `manifest.test.ts:252-254` says so in its own words:
  *"nothing else catches this, because no CI leg builds these images"*, and
  `grep -nE "docker build|frontend-images|lance-(home|lakehouse|media|annotator)" .github/workflows/*.yml`
  returns no build.

So the only place the four zones ever meet behind one origin is
`scripts/verify_cross_zone_oidc.sh` — which cannot run (2.5) and is not wired into any Makefile target
or workflow (`grep -n "verify_cross_zone_oidc" Makefile .github/workflows/*.yml` → no matches).

Verdict: **DEVIATES** (no reason recorded). This is the root cause of 2.2 and 2.5 both: the gates
that exist are excellent *static* gates, and there is no *composed* gate to catch a link that
type-checks, lints, unit-tests, renders, and 404s.

Mitigation that fits the existing design: extend the zone-contract gate to resolve every absolute
`<a href>` against the union of (zone base paths) ∪ (each zone's built route ids). Both inputs are
already available — `manifest.ts:42,70,102` and `build/server/manifest.js`, which
`budget.test.ts` already reads per zone from `.svelte-kit/output`.

---

## 5. Independent deployability — proven, with named limits

### 5.1 What is actually independent — CONFORMS (evidence, not opinion)

**Per-zone image.** `.docker/frontend.dockerfile:26` `ARG APP=home`; `:49`
`bun run --cwd components/frontends/${APP} build`; `:80-83` copies only
`/src/components/frontends/${APP}` into the runtime stage. Driven per zone by
`Makefile:103-107`:

```
frontend-images:
	@for z in $(ZONES); do \
	  docker build $(BUILD_ARGS) --build-arg APP=$$z -f .docker/frontend.dockerfile -t lance-$$z:dev . || exit 1; \
	done
```
with `Makefile:41` `ZONES := home lakehouse media annotator` (gated against drift by
`manifest.test.ts:232-248`).

**Per-zone Deployment + Service.** `chart/templates/frontends.yaml:17` `{{- range $fe.apps }}` emits
one Deployment (`:21`) and one Service (`:92`) each, named `web-<name>` (`:18`) — with the collision
reason recorded at `:11-16` ("the bare `<name>` would collide with backend services that share a
name"). Rendered:

```
$ helm template lance chart --set ingress.enabled=true --set media.enabled=true | grep 'image: "lance-'
          image: "lance-home:dev"
          image: "lance-lakehouse:dev"
          image: "lance-media:dev"
          image: "lance-annotator:dev"
```

**Per-zone rollout, proven by render.** `chart/templates/_helpers.tpl:28-40` `lance.frontendImage`
resolves a per-app `tag` over `frontend.image.tag`, with the *reason* at `:30-33`:

> "A zone boundary costs a full document load, and the only thing it buys is INDEPENDENT DEPLOY —
> which a single shared tag silently cancels."

I exercised it with an override values file:

```
$ cat pertag.yaml
frontend:
  apps:
    - { name: home, catchAll: true }
    - { name: lakehouse, path: /lakehouse, tag: "v9.9.9-lakehouse-only" }
    - { name: media, path: /media }
    - { name: annotator, path: /annotator }

$ helm template lance chart -f pertag.yaml | grep 'image: "lance-'
          image: "lance-home:dev"
          image: "lance-lakehouse:v9.9.9-lakehouse-only"     ← one zone moved, three did not
          image: "lance-media:dev"
          image: "lance-annotator:dev"
```

**Per-zone turbo tasks.** All tasks in `frontend/turbo.json:6-52` are package tasks, so `--filter`
narrows the graph:

```
$ cd frontend && bunx turbo run build --filter=media --dry=json
packages in scope: ['media']
 task: media#build | deps: ['@repo/api#build','@repo/config#build','@repo/labeling#build','@repo/media-api#build','@repo/ui#build']
```
No other zone appears. Each zone also has its own `test:e2e`, `check`, `lint`, `fmt:check`
(`components/frontends/*/package.json`).

Verdict: **CONFORMS**. Per-zone build, per-zone image, per-zone Deployment/Service, per-zone tag,
per-zone task graph — all demonstrated, none assumed.

### 5.2 Limit 1 — builds are not independent even though deploys are — DEVIATES

`.docker/frontend.dockerfile:41-44` copies **the whole workspace** into every zone's builder:

```
COPY frontend/package.json frontend/bun.lock frontend/turbo.json ./
COPY frontend/patches patches
COPY frontend/packages packages
COPY frontend/components/frontends components/frontends     ← all four zones
```

So editing one line in `annotator/src` busts the docker layer cache for the `lakehouse` image too, and
`make frontend-images` rebuilds all four from that layer down. The reason for the broad copy is real
and recorded (`:37-40`: `bun install --frozen-lockfile` fails with "Workspace not found" if a member
is absent), and `manifest.test.ts:269-273` gates it — but no narrower variant (per-zone
`COPY components/frontends/${APP}` after a lockfile-only install) was attempted. Verdict:
**DEVIATES** — the constraint is documented, the *acceptance of the cache cost* is not.

### 5.3 Limit 2 — the shared layer forces lockstep rebuilds — DEVIATES-WITH-REASON

Six of seven shared packages export raw `src/*.ts` (see 4.3), and `@repo/ui` is bundled into each
zone's output (`.docker/frontend.dockerfile:47-49`, and `frontend/README`/dockerfile note
"`@repo/ui` + `@repo/api` are BUNDLED into the zone's build/"). A change to `@repo/api` or `@repo/ui`
therefore requires rebuilding and redeploying **all four** zones — a change to the shared *shell* is
a whole-estate release. That is inherent to build-time composition of the shared layer
(`composition.md:31`: "a slice change is only live once the shell re-installs, re-bundles, and
redeploys"), and the packages in question are the cross-cutting ones the skill sanctions
(`communication.md:133`: "auth/session, theme, feature flags"). Verdict:
**DEVIATES-WITH-REASON**, though the reason is inferred from the skill rather than written in the repo.

### 5.4 Limit 3 — the default is a single shared tag — DEVIATES-WITH-REASON

`chart/values.yaml:553` `tag: dev` with no per-app override in the committed
`frontend.apps` (`:562-573`). So *as shipped*, all four zones move together and the independence
proven in 5.1 is a capability, not a practice. `values.yaml:550-552` states the tradeoff explicitly:

> "A zone may override it with `tag:` on its frontend.apps entry below — that is what makes the zone
> split mean anything: independent deploy is the only thing a zone boundary buys, and one shared tag
> across every zone cancels it."

Verdict: **DEVIATES-WITH-REASON** — the reason is written down, and the mechanism exists; the practice
does not yet.

### 5.5 Limit 4 — no image build or release pipeline in CI — DEVIATES

`grep -nE "docker build|frontend-images|lance-(home|lakehouse|media|annotator)" .github/workflows/*.yml`
returns nothing. `.github/workflows/ci.yml` has jobs `test, frontend, lineage-e2e, e2e-stack, ray-e2e,
auth-e2e` — none builds or publishes a zone image. `manifest.test.ts:252-254` concedes the
consequence in its own comment: *"nothing else catches this, because no CI leg builds these images"*,
and the fix chosen was a static assertion about the dockerfile's `COPY` lines rather than an actual
build. So "independently deployable" is true of the chart and the dockerfile, and untested by any
pipeline. Verdict: **DEVIATES**.

### 5.6 What IS well covered on the aggregate-cost axis — CONFORMS

`principles.md:119` ("Monitor aggregate bundle size") is satisfied better than the skill asks:
`frontend/packages/zone-contract/src/budget.test.ts` + `budget.json` set **per-zone gzipped ceilings
split into `staticGzipKB` (entry closure) and `deferredGzipKB` (dynamic-import-only)**, read from
Vite's own manifest rather than regex (`budget.test.ts:37-40`), with the ordering hazard fixed and
pinned (`turbo.json:24-28` `@repo/zone-contract#test` dependsOn every `<zone>#build`, asserted by
`manifest.test.ts:212-230`). The reasoning at `budget.test.ts:18-35` is exactly the failure mode the
skill warns about, caught and corrected (annotator read 4179/4800 KB while its real entry cost was
324 KB). Current ceilings/measurements from `budget.json`:

| zone | staticGzipKB ceiling | measured | deferredGzipKB ceiling | measured |
|---|---|---|---|---|
| home | 200 | 157 | 40 | 1 |
| lakehouse | 600 | 490 | 120 | 46 |
| media | 1050 | 927 | 120 | 46 |
| annotator | 420 | 324 | 4200 | 3854 |

---

## 6. Remaining skill principles, checked

### 6.1 Team prefixes on shared global namespaces — mostly CONFORMS, one latent hazard

Four zones share **one origin**, so `localStorage` is one namespace
(`principles.md:53-64` requires prefixed storage keys).

```
$ grep -rn "localStorage" --include=*.ts --include=*.svelte packages/*/src components/frontends/*/src
```
Every key literal is prefixed:

| Key | Site |
|---|---|
| `mode-watcher-mode` | `components/frontends/annotator/src/app.html:29` — deliberately shared, one origin-wide theme (`annotator/src/routes/+layout.svelte:27-33` gives the reason: the zone used to read its own `lance-media-theme` key "which is why it rendered dark against a light estate") |
| `lance-media:saved-views` | `media/src/lib/saved-views.svelte.ts:15` |
| `lance-media-table-cols-v6`, `lance-media-doc-cols-v1` | `media/src/routes/+page.svelte:267,337` |
| `lance-media-atlas-cols-v2` | `media/src/lib/atlas/mount-atlas.svelte:38` |
| `lance-media-workflow-graph-v1` | `media/src/lib/workflow/graph.svelte.ts:58` |
| `lance-media-hidden-cols` | `media/src/lib/components/filter-popover.svelte:63` |
| `lance-media-{search-map-vsplit,workflow-split,tree-split,atlas-vsplit,atlas-split}` | media split panes |
| `lance-media-annotate` | `annotator/src/lib/viewer/layout/AnnotatorShell.svelte:124` |

Latent hazard: `packages/ui/src/lib/components/resizable-split/resizable-split.svelte:29` defaults
`storageKey = 'lance-media-split'` — an **un-zoned default in a shared component on a shared origin**.
Every current call site passes an explicit key
(`grep -rn "storageKey=" --include=*.svelte components/frontends/*/src` → 6 hits, all explicit), so
there is no live collision; the next caller that omits it in a second zone silently shares one
persisted pane fraction across zones. Verdict: **CONFORMS today, latent** — the prefix convention is
followed but not mechanically enforced (`principles.md:72`: "put the prefix in a lint rule … so a
violation fails the build").

Secondary note: the prefix in use is `lance-media-`, i.e. the *pre-merge product* name, applied by
both the `media` and `annotator` zones. It is not a per-zone prefix, so it does not distinguish which
zone owns a key. Harmless while media and annotator are the only writers.

### 6.2 Cross-slice communication — N/A, correctly

```
$ grep -rn "new CustomEvent\|dispatchEvent\|window.addEventListener" --include=*.ts --include=*.svelte packages/*/src components/frontends/*/src
(no matches)
```
There is **no client-side cross-slice messaging at all** — which is the right answer for routing-based
composition: two zones are never on the page at the same time, so `communication.md`'s event-bus
patterns do not apply. Shared state travels the two channels the skill calls most-decoupled: the
**URL** (`communication.md:137`) and a **cookie** (`chart/values.yaml:574-575`, one sealed
origin-wide session). Verdict: **CONFORMS**.

### 6.3 Resilience / per-slice observability — CONFORMS

- Per-zone error attribution on both sides:
  `home/src/hooks.server.ts` `makeZoneServerErrorHandler('home')` and
  `home/src/hooks.client.ts` `makeZoneClientErrorHandler('home')`, with the correct rationale
  (`hooks.client.ts`: "the ONLY party that can report this zone's own hydration or
  client-navigation failure — the server never sees one"). Demonstrated live in 2.2's addendum:
  `[zone-error] {"zone":"home",…}`.
- A zone being down 502s only its own paths — inherent to the Ingress model
  (`routing-and-orchestration.md:31`), and the dev proxy reproduces it deliberately with a helpful
  message (`packages/zone-contract/src/proxy.ts:93-99`).
- Signed-out / forbidden are rendered as *answers*, not errors
  (`packages/ui/src/lib/shell/forbidden-page.svelte:5-7`: "a 403 is an answer, not an error").
- SSR-first with per-request session hydration in every zone (`makeZoneHooks`), so a zone whose JS
  fails still renders — `principles.md:92`.

### 6.4 Governed design system — CONFORMS

One versioned component library (`@repo/ui`, 36 subpath exports incl. `./styles/tokens.css`
— `python3 -c "import json;print(len(json.load(open('packages/ui/package.json'))['exports']))"`) consumed
by all four zones, on `bits-ui` headless primitives. `principles.md:31` satisfied.

---

## Verdict table

| # | Finding | Verdict | Severity |
|---|---|---|---|
| 2.2 | Four cross-zone links target `/data` / `/lineage` — no zone serves them; proven 404 against the built home zone. Includes `@repo/ui` project-switcher (3 of 4 zones, every page) and the home landing grid (front door). | **BUG** | high |
| 2.5 | `scripts/verify_cross_zone_oidc.sh:33` probes `/data` for readiness → the only composed-page verification in the repo cannot pass. | **BUG** | high |
| 2.3 | `AuditViewer.svelte:341` hard-navigates a same-zone `${base}/data/...` link; `e2e/admin/audit.spec.ts:103` pins the wrong contract, contradicting `e2e/admin/tenants.spec.ts:72`. | **BUG** | medium |
| 2.6 | No CI leg ever composes the four zones behind one origin (`e2e_stack.sh:110` sets `frontend.enabled=false`; per-zone Playwright only). Root cause of 2.2 + 2.5. | **DEVIATES** | medium |
| 5.5 | No CI job builds or publishes a zone image; independence is asserted statically. | **DEVIATES** | medium |
| 5.2 | Dockerfile copies all four zones into every zone's builder → per-zone builds share a cache fate. | **DEVIATES** | low |
| 3.1 | `ingress.yaml:5` and `_helpers.tpl:27` still name the pre-merge `/data,/lineage,/models,/admin` zones. | **DEVIATES** | low |
| 4.5 | `loginHref` re-derived in 23 files. | **DEVIATES** | low |
| 6.1 | `resizable-split.svelte:29` un-zoned default `storageKey` on a shared origin (latent). | **CONFORMS today** | low |
| 1.1 | No zone imports another zone's `src/`. | **CONFORMS** | — |
| 1.2 | Vertical slices: each zone owns its own BFF; the root-absolute `/api` side door was removed. | **CONFORMS** | — |
| 1.3 | `lakehouse` merges four ex-zones — reason in `values.yaml:563-566`. | **DEVIATES-WITH-REASON** | — |
| 2.1 | Cross-zone nav is a full document load, enforced by a Svelte-compiler AST gate, softened by `<link rel=prefetch>`. | **CONFORMS** | — |
| 2.4 | All 53 distinct href forms except 2.2's four are correct. | **CONFORMS** | — |
| 3 | Declaration chain (microfrontends.json ↔ paths.base ↔ values.yaml ↔ rendered Ingress ↔ vite ports) agrees, machine-checked, 278 tests green. | **CONFORMS** | — |
| 4.1 | `turbo boundaries`: 2925 files, 11 packages, no issues. | **CONFORMS** | — |
| 4.2 | Internal graph is one edge (`@repo/labeling → @repo/media-api`); no cycles. | **CONFORMS** | — |
| 4.3 | `@repo/ui` has zero `@repo/*` imports; `Me` mirrored structurally with reason — but nothing pins the two shapes. | **DEVIATES-WITH-REASON** | low |
| 4.4 | Estate IA (22 rows) lives in `@repo/ui` nav-config: shell-owns-top-level-routing, sidebar two-level and zone-owned. | **DEVIATES-WITH-REASON** | — |
| 5.1 | Per-zone image / Deployment / Service / tag / turbo filter all demonstrated by render + dry-run. | **CONFORMS** | — |
| 5.3 | Shared-package source bundling forces four-zone rebuilds on a shared change. | **DEVIATES-WITH-REASON** | — |
| 5.4 | Committed default is one shared tag `dev`; independence is a capability, not a practice — tradeoff written at `values.yaml:550-552`. | **DEVIATES-WITH-REASON** | — |
| 5.6 | Per-zone gzipped bundle budgets, split static vs deferred, read from Vite's manifest, correctly ordered after the builds. | **CONFORMS** | — |
| 6.1–6.4 | Storage-key prefixes, no cross-slice bus (correct for zones), per-zone error attribution, one design system. | **CONFORMS** | — |

## Things I could not verify

- **No live cluster drive.** I did not deploy to kind, so I did not observe the Ingress serving the
  four zones together. The 404s in 2.2 were proven against the built `home` zone directly plus the
  *rendered* Ingress rules — which is what determines the outcome — but the composed path itself is
  untested by me and (per 2.6) by CI.
- **Asset-prefix collision** is argued from `build/client/lakehouse/_app/immutable/...` existing
  (`ls frontend/components/frontends/lakehouse/build/client` → `lakehouse`) rather than from two zones
  being served side by side.
- Whether `manifest.test.ts`'s gates each actually *fail* on their violation (the repo has open task
  GC2(c) for exactly this) — I ran them green, I did not mutate anything to prove they bite.

## Working-tree note (not a finding)

This audit edited nothing. `git status --porcelain` at the end of the run reports
`M frontend/packages/api/tests/oidc.test.ts` — a change that was **not** made by this audit (only
read-only greps, `helm template`, `bunx turbo`, `bunx vitest`, and one `bun ./build/index.js` were
run). It appeared during the run, so something else is writing to this working tree concurrently.
Recording it so it is not mistaken for audit damage. Its content (hardening two weak assertions in
the session-seal test) is unrelated to micro-frontend composition. A later
`git status --porcelain` returned empty again, so the concurrent writer committed or reverted it
mid-audit — another reason to treat any single working-tree observation here as a point-in-time read.

---

# Verification

Adversarial re-verification of the five most consequential claims (2.2, 2.5, 2.3, 2.6, 5.5). Every
cited `path:line` was re-opened independently; the 404 probe was re-run from scratch rather than
trusted. Default was "refuted unless the code says it".

**Tree drift, recorded first.** The report says "clean at `e489f2b`". `git log --oneline -1` at
verification time is **`dfa95f9`** ("docs(goal): record the flaky sealed-cookie assertion in the pull
tracker"), working tree clean. `bunx vitest run --root packages/zone-contract` now reports
**283 passed (283)**, not the 278 the report quotes. The concurrent writer the report flagged in its
own working-tree note kept moving. Every line citation below was re-checked at `dfa95f9` and still
resolves; the test count is the only number that went stale.

## Claim 1 — 2.2: four cross-zone links target paths no zone serves → 404. **CONFIRMED**

Line citations re-read, all exact:

| cited | actual content at that line |
|---|---|
| `packages/ui/src/lib/shell/project-switcher.svelte:55` | `<a href="/data" data-sveltekit-reload class="flex w-full items-center gap-2 px-2 py-1.5">` ✓ |
| `components/frontends/home/src/routes/+page.svelte:25` | `<a href={`/data/projects/${p.project}`} data-sveltekit-reload class="group block">` ✓ |
| `lakehouse/src/lib/data/TableDetail.svelte:930`, `:938` | `href="/lineage" data-sveltekit-reload` ✓ (two separate `<a>`) |
| `lakehouse/src/lib/models/PipelineControl.svelte:86` | `<a href="/lineage" data-sveltekit-reload>Lineage</a>` ✓ |

Ownership re-derived independently, not taken from the report:
- `home/build/server/manifest.js` route ids = exactly `/`, `/auth/callback`, `/auth/login`,
  `/auth/logout`, `/capi/v1/projects`. Nothing under `/data` or `/lineage`.
- `home/src/routes/` on disk holds only those five plus `+error.svelte`/`+layout*`/`+page*` — there is
  **no `[...rest]` catch-all** that could absorb `/data`.
- I read `chart/templates/ingress.yaml` in full looking for a rewrite, a legacy-path rule, or an
  nginx annotation that would redirect `/data`. There is none: non-`catchAll` apps emit their own
  `path`, `catchAll` emits `/`, `auth.enabled` adds `/dex`, and a comment block explicitly records the
  removal of the root-absolute `/api` rule. `/data` can only land on `home`.
- `chart/values.yaml:562-573` `frontend.apps` = `home(catchAll)`, `lakehouse:/lakehouse`,
  `media:/media`, `annotator:/annotator`. No `/data`.

**Independently re-ran the probe** (fresh server on a different port, not the report's):

```
$ cd frontend/components/frontends/home && PORT=5417 HOST=127.0.0.1 bun ./build/index.js &
/                        -> 200
/data                    -> 404
/data/projects/acme      -> 404
/lineage                 -> 404
/models                  -> 404
/admin                   -> 404
/auth/login              -> 302
/lakehouse/data          -> 404
```

Blast radius re-checked and confirmed: `app-shell.svelte:7` imports and `:107` renders
`<ProjectSwitcher project={shellProject} />` **unconditionally** inside the row-1 chrome; `AppShell` is
imported by `home/src/routes/+layout.svelte:7`, `lakehouse/…:8`, `media/…:9` — three of four zones
(annotator does not use it). Matches the report exactly.

**Completeness of the enumeration — I tried to break it and could not.** The report's grep pattern only
covers `href="` and `href={\``. I re-swept with a wider pattern covering single quotes, `{'…'}`,
component `href=` props and `goto()`:

```
$ grep -rnE "(href|goto\()\s*=?\s*[\{\"'\`]+\s*/(data|lineage|models|admin)([/\"'\`}]|\$)" \
    --include=*.svelte --include=*.ts packages/*/src components/frontends/*/src
$ grep -rnE "goto\(['\"\`]/" … → (no matches)
```
Returns the **same five hits, no more**. The enumeration is complete.

Two precision defects in the report's own framing (finding stands, wording does not):
- **"four cross-zone links" is five links across four sites.** `TableDetail.svelte:930` and `:938` are
  two distinct `<a>` elements; the table groups them as site (c). The verdict-table headline
  undercounts by one.
- **§2.2 prose says "`/data`, `/lineage`, `/models`, `/admin` … four links still use the pre-merge
  form"**, which reads as if all four prefixes are in use. Only `/data` and `/lineage` appear as bare
  absolute hrefs — `/models` and `/admin` have zero occurrences (my sweep above). The verdict table
  gets this right; the prose does not.
- **Blast-radius rhetoric is inflated.** "the single most persistent control in the estate is a 404 on
  every page of three zones" — the control *renders* on every page, but the `/data` link is the
  **current-project row inside a closed dropdown**, marked with `<Check class="ml-auto size-4" />`
  (project-switcher.svelte:60). It 404s when a user opens the switcher and clicks the row they are
  already notionally on. Real bug; not a visibly broken control on every page.

Verdict on the claim: **CONFIRMED** (BUG, high). Cited lines exact, 404 independently reproduced,
enumeration complete, no redirect shim anywhere on the path.

## Claim 2 — 2.5: `verify_cross_zone_oidc.sh` readiness probe is `/data`, so the script cannot pass. **CONFIRMED**

`scripts/verify_cross_zone_oidc.sh:32-36` re-read verbatim; the report's quote is accurate:

```
for i in $(seq 1 30); do
  w=$(curl -s -o /dev/null -w '%{http_code}' -m2 "http://localhost:$ORIGIN_PORT/data" 2>/dev/null || true)
  …
  { [ "$w" = "200" ] || [ "$w" = "308" ]; } && [ "$d" = "200" ] && { … break; }
  [ "$i" = "30" ] && { … fail "port-forwards never became ready"; }
```

I checked the two ways this could survive and both fail:
1. **`set -uo pipefail` has no `-e`** — but `fail()` is `echo …; exit 1`, an explicit exit. The script
   dies. Not survivable.
2. **Could `/data` return 308 under the live OIDC-on stack the script targets?** There *is* a
   login-first gate (`packages/api/src/bff.ts:117-123`) that returns a redirect for signed-out page
   navigations — a plausible escape. Refuted on two counts: it is **302, not 308** (fails the
   comparison either way), and `isGatedPageRequest` (`bff.ts:80-92`) requires
   `accept: text/html`, which this bare `curl` does not send — so the gate never fires and the
   response is the plain 404 I reproduced. The probe fails under both auth-off and auth-on.

**Corroboration the report did not use, and it is damning.** `manifest.test.ts:278-282` documents that
this exact bug class was already known:

> `packages/rask-ui`, chart/templates named `@rask/api`, and scripts/verify_cross_zone_oidc.*
> navigated to `/data` and `/models/pipeline`. None of it is type-checked, none of it is linted,
> and all of it is on the deploy path …

`SEARCHED` includes `scripts/verify_cross_zone_oidc.sh`, but
`DEAD = ['@rask/', '@lance/', 'packages/rask-ui', 'components/frontends/data']` matches no bare
`/data` URL. The repo wrote a gate *for this file, for this defect*, and the residual `/data` probe
slipped past it. Confirms the report's claim about the DEAD list, with stronger evidence than it gave.

Also confirmed: `grep -rn "verify_cross_zone_oidc" Makefile .github/workflows/` → **no matches**. The
script is wired into nothing.

Verdict: **CONFIRMED** (BUG, high).

## Claim 3 — 2.3: `AuditViewer.svelte:341` hard-navigates a same-zone link and an e2e test pins it. **CONFIRMED, and stronger than reported**

Re-read all four citations; every one is exact.

- `AuditViewer.svelte:170-177` `resourceHref()` builds `${base}/data/tables/…`,
  `${base}/data/namespaces/…`, `${base}/data/warehouses` ✓
- `AuditViewer.svelte:340-341`:
  `<!-- Cross-zone jump: leaves this zone's route manifest, so hard-navigate. -->` +
  `<a class="btn jumplink" href={resourceHref(drawerEvent.resource)} data-sveltekit-reload>` ✓
- `lakehouse/build/server/manifest.js` contains `id: "/data/tables/[table]"`,
  `id: "/data/namespaces/[id]"`, `id: "/data/warehouses"` — the targets **are** in this zone's
  manifest, so the comment's premise is false ✓
- `e2e/admin/audit.spec.ts:100-103` asserts `data-sveltekit-reload` present ✓;
  `e2e/admin/tenants.spec.ts:70-72` asserts `.not.toHaveAttribute('data-sveltekit-reload', '')` for the
  same navigation class ✓
- `cross-zone-reload.ts:13-15` (exact lines) states the contract the code violates ✓

**I specifically tested the "intended behaviour with a comment explaining it" defence and it is
refuted by the file itself.** The report cited only the stale comment at `:340`. The docstring on the
function that *builds* these hrefs, 170 lines earlier, already says the opposite:

```
AuditViewer.svelte:168-169
	/** Map an audit resource id to its estate page, when one exists. These target the catalog AREA of
	 *  this same zone, so they are base-relative soft navigations rather than cross-zone hard navs. */
```

So the file asserts "same-zone soft navigation" at `:169` and "cross-zone, hard-navigate" at `:340`
about the identical set of URLs. This is not a documented deviation — it is one file contradicting
itself, with the attribute following the wrong half. Verdict **BUG** stands, on stronger grounds than
the report presented.

Why the gate misses it, verified in source: `hrefToPath` (`cross-zone-reload.ts:37-58`) renders `{base}`
as the `EXPR` sentinel `￿`, `ZONE_PATH` (`:23`) is anchored `^\/(lakehouse|media|annotator)(?:\/|$)`,
and `findViolations` only pushes when `isCrossZonePath(path) && !hasReloadEnabled(attrs)`. There is no
inverse rule, so a same-zone link that *does* reload is unreachable by this gate. The test at
`cross-zone-reload.test.ts:25-28` is present and reads as the report quotes it.

Verdict: **CONFIRMED** (BUG, medium).

## Claim 4 — 2.6: no CI leg composes the four zones behind one origin. **CONFIRMED, with one overstated sentence**

Re-verified each sub-citation:
- `.github/workflows/ci.yml` jobs are exactly `test, frontend, lineage-e2e, e2e-stack, ray-e2e,
  auth-e2e` (`grep -nE '^  [a-z0-9-]+:$'` → lines 8, 60, 118, 143, 183, 213). Matches the report.
- `ci.yml:102` is `bunx turbo run test:e2e --concurrency=1`, and the surrounding comment confirms
  per-zone isolation in the repo's own words: *"each zone spins its own vite SSR dev server + a
  chromium instance"* ✓
- **I checked whether any zone's Playwright config secretly composes others.** `lakehouse` has three
  `command:` entries (`playwright.config.ts:33,39,51`), which looked like a possible refutation — but
  they are its own dev server auth-off, its own dev server auth-on, and `e2e/admin/mock-catalog.ts`.
  No other zone's server. home/media/annotator have one each. Refutation attempt failed; claim holds.
- `scripts/e2e_stack.sh:110` is `--set frontend.enabled=false` ✓ — verified at the exact line.
- No CI job builds a zone image (see Claim 5).

**One sentence in 2.6 is wrong.** The report writes: *"the only place the four zones ever meet behind
one origin is `scripts/verify_cross_zone_oidc.sh`"*. `frontend/package.json:13` is
`"dev": "turbo run dev dev:proxy"`, and `packages/zone-contract/src/proxy.ts` fans all four zones
behind one port — so `bun dev` composes them locally, every day. The report credits this proxy itself
in §3, so 2.6's phrasing contradicts its own §3. The **finding** is unaffected: `dev:proxy` appears in
no test, no Playwright config and no workflow (`grep -rn "dev:proxy\|PROXY_PORT" Makefile Tiltfile
.github/workflows/` → nothing), so there is still no *automated composed gate*. Headline confirmed,
one supporting sentence refuted.

Verdict: **CONFIRMED** (DEVIATES, medium) — with the "only place" sentence **REFUTED**.

## Claim 5 — 5.5: no CI job builds or publishes a zone image. **CONFIRMED**

```
$ grep -rnE "docker build|frontend-images|lance-(home|lakehouse|media|annotator)" .github/workflows/
.github/workflows/ci.yml:142:  # … untrusted docker builds on forked PRs …
.github/workflows/ci.yml:212:  # … to avoid untrusted-code docker builds …
```
Two hits, both **prose inside comments**; no build step, no `docker build`, no `make frontend-images`,
no registry push. The build path exists only in `Makefile:103-107` (`frontend-images`, loop over
`ZONES := home lakehouse media annotator` at `Makefile:41`) and `Makefile:109-110` (`frontend-load`),
neither invoked by any workflow.

The report's quote from `manifest.test.ts:250-255` is accurate — the repo concedes it in a comment:
*"nothing else catches it, because no CI leg builds the images"*, with the `eslint-rules` incident
recorded as the precedent (*"every zone image build had been broken since it was added"*).

Verdict: **CONFIRMED** (DEVIATES, medium).

## Line-citation errors found

Two, both cosmetic, neither changing a verdict:

| report | actual | note |
|---|---|---|
| `chart/templates/ingress.yaml:5` | `:4` | the `/data,/lineage,/models,/admin` stale comment is on line 4 (`grep -n 'domain zone owns its base path'`). Content verbatim as quoted. |
| "278 tests green" | **283** | tree moved `e489f2b` → `dfa95f9` during/after the audit. |

`chart/templates/_helpers.tpl:27` (`Call: include "lance.frontendImage" (list $root "data")`) is
**exact** — verified by `grep -n 'Call: include'`.

## Unfalsifiable or unbacked claims

I looked for these specifically and found the report unusually clean. Every finding I checked carries a
runnable command. Three softer spots, all of which the report itself flags:

1. **§1.3's caveat** — *"That is a legitimate but different justification than the skill's, and it is
   not written down anywhere in the repo"* — is a negative-existence claim with no command. Weakly
   unfalsifiable, but it is a caveat, not a verdict.
2. **§6.1's "latent hazard"** — graded "CONFORMS today, latent". Correctly labelled as
   not-currently-failing; the prediction ("the next caller that omits it…") is by nature untestable.
3. **The report's own "Things I could not verify"** discloses the three real gaps (no live cluster
   drive, asset-prefix collision inferred from a directory listing, gates run green but never mutated
   to prove they bite). Disclosed rather than papered over.

## Tally

**5 of 5 top claims CONFIRMED. 0 refuted at the finding level.** Refuted at the supporting-detail
level: 2.6's "the only place the four zones ever meet behind one origin" sentence; 2.2's "four links"
count (it is five), its `/models`+`/admin` prose, and its "404 on every page" blast-radius rhetoric;
plus one off-by-one line cite and a stale test count.

Two claims came out **stronger** than the report argued them: 2.3 (the same file contradicts itself at
`:169` vs `:340`, so the "intended behaviour, comment explains it" defence is closed) and 2.5
(`manifest.test.ts:278-282` shows the repo already wrote a gate for this exact file and this exact
defect, and the residual probe slipped through the `DEAD` list).

**Most important surviving finding: 2.2.** Five `<a>` elements hard-navigate to `/data`,
`/data/projects/<p>` and `/lineage` — paths the rendered Ingress sends to the catch-all `home` zone,
whose route manifest holds five ids and no catch-all. Independently reproduced 404 on a fresh server.
One of the five lives in `@repo/ui`'s shared shell and ships to three of four zones; another is the
home landing page's project grid. 2.5 is its dependent: the one artifact that would have caught it
cannot start, because its own readiness probe is one of the dead paths.
