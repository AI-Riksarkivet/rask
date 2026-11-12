<script lang="ts">
	import { resolve } from '$app/paths';
	// `/projects/<p>` — ONE project: its metadata and overview (the warehouses claiming it, its
	// effective admins). A project is the top of the hierarchy — project › warehouse › namespace ›
	// table — so the page describing one is a top-level page in the main menu, and every rung BELOW it
	// links to its catalog page.
	//
	// Gated by the catalog; every degrade state is named honestly rather than collapsed into "empty".
	import { AlertTriangle, FolderKanban, RefreshCw, ShieldAlert, Trash2 } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { ProjectHierarchy, type HierarchyHref } from '#lib/flow/hierarchy/index.ts';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import type { ProjectSummary } from '#lib/catalog.ts';
	import { policyRequestFrom, type PolicyDraft, type ProjectPolicies } from '#lib/policies.ts';
	import ProjectDeleteDialog from '#lib/ProjectDeleteDialog.svelte';
	import ControlEventLine from '#lib/admin/ControlEventLine.svelte';
	import { Subject } from '#lib/access/identity/index.ts';
	import { isEstateAdmin } from '#lib/estate-admin.ts';
	import type { ControlEvent } from '#lib/admin/control.ts';
	import {
		deleteProjectPolicy,
		fetchProjectPolicies,
		setProjectPolicy,
	} from '#lib/remote/policies.api.ts';
	import {
		fetchProject,
		namespaceTables,
		projectEvents,
		warehouseNamespaces,
	} from '#lib/remote/warehouses.api.ts';

	const project = $derived(page.params.project ?? '');

	// The graph is a map: every rung below the project links to the catalog page that owns it.
	// `project` returns null: from `/projects/<id>`, the project rung IS this page.
	const rungHref: HierarchyHref = (rung) => {
		if (rung.tier === 'project') return null;
		if (rung.tier === 'warehouse')
			return resolve('/catalog/warehouses/[id]', { id: encodeURIComponent(rung.id) });
		if (rung.tier === 'namespace')
			return resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(rung.id) });
		return resolve('/catalog/tables/[table]', { table: encodeURIComponent(rung.id) });
	};

	let detail = $state<ProjectSummary | null>(null);
	// #71 the project's 10 most recent control events. `denied` is its own quiet state — the events
	// feed is estate-admin gated at the catalog, and a project admin who is not estate admin should
	// see a page that simply omits the panel's content, not an error.
	let events = $state<ControlEvent[] | null>(null);
	let eventsDenied = $state(false);
	let lastStatus = $state(0);
	let settled = $state(false);
	let deleting = $state(false);

	const unauthorized = $derived(detail === null && settled && lastStatus === 401);
	// A 403 and a 404 read the same: the catalog keeps a project's existence to itself.
	const missing = $derived(detail === null && settled && [403, 404].includes(lastStatus));
	const offline = $derived(
		detail === null && settled && ![200, 401, 403, 404].includes(lastStatus),
	);

	// #65 the tenant's maintenance plane. ONE read answers all three tiers: the project's own record
	// plus every table/namespace record inside it — a per-object `policy/describe` could not have been
	// issued anyway, since the page would have to already know each id.
	//
	// The states mirror the lakehouse's policy cards, and `unavailable` is the load-bearing one: a
	// failed read must never render as "no policy", because that reads as an invitation to Save — which
	// would overwrite a live record from a draft built on nothing.
	let policies = $state<ProjectPolicies | null>(null);
	let policiesPart = $state<'loading' | 'ok' | 'denied' | 'unavailable'>('loading');
	let policiesDenied = $state<string | null>(null);
	let policyError = $state<string | null>(null);
	let editingPolicy = $state(false);
	let busy = $state(false);
	// number | null is what bind:value on a type="number" input delivers (Svelte 5 coerces, or null for
	// an empty field) — and null is exactly what "inherit" means to `policyRequestFrom`, which OMITS it.
	let draft = $state<PolicyDraft>({
		retention_days: null,
		retain_versions: null,
		interval: null,
		target: null,
		enabled: true,
	});

	/** The project's OWN record, if it has one — the tier this page can edit. */
	const ownPolicy = $derived(
		policies?.policies.find((p) => p.kind === 'project' && p.id === project) ?? null,
	);
	/** Everything below it, which SHADOWS it dataset-by-dataset. Read-only here: each belongs to the
	 *  lakehouse rung that owns it, and every row links there. */
	const scopedPolicies = $derived((policies?.policies ?? []).filter((p) => p.kind !== 'project'));

	// The events feed is estate-wide and estate-admin gated at the catalog, so it is read only for an
	// estate admin: anyone else would be refused on every visit.

	/** The project's record first; its policies and events only once there is a project to read them
	 *  for, since a project the catalog does not show this caller has neither. */
	async function load(): Promise<void> {
		const current = project;
		const res = await fetchProject(current);
		if (project !== current) return; // latest-wins across navigation
		settled = true;
		if (!res.ok) {
			detail = null;
			lastStatus = res.status;
			return;
		}
		detail = res.data;
		lastStatus = 200;
		void loadPolicies();
		if (!isEstateAdmin(page.data.me)) {
			eventsDenied = true;
			return;
		}
		const evRes = await projectEvents({
			project: current,
			warehouses: res.data.warehouses.map((w) => w.id),
		});
		if (project !== current) return;
		events = evRes.ok ? evRes.data : null;
		eventsDenied = !evRes.ok && (evRes.status === 401 || evRes.status === 403);
	}

	async function loadPolicies(): Promise<void> {
		const current = project;
		try {
			const res = await fetchProjectPolicies(current);
			if (project !== current) return; // latest-wins across navigation
			if (res.ok) {
				policies = res.data;
				policiesPart = 'ok';
			} else if (res.status === 401) {
				policiesDenied = 'Sign in to view this project’s maintenance policies.';
				policiesPart = 'denied';
			} else if (res.status === 403) {
				policiesDenied = 'Denied: maintenance policies need project-admin access.';
				policiesPart = 'denied';
			} else {
				policiesPart = 'unavailable';
			}
		} catch (err) {
			// The parse boundary throws on a wire-contract drift — surface it, never render from a lie.
			if (project !== current) return;
			policyError = `policy response drifted from the contract: ${String(err)}`;
			policiesPart = 'unavailable';
		}
	}

	function startPolicyEdit(): void {
		// Seeded from the STORED record, so an untouched field round-trips its own value rather than
		// the model's default (`_record` merges on `exclude_unset`, but a form that echoed defaults back
		// would be SETTING them).
		draft = {
			retention_days: ownPolicy?.retention_days ?? null,
			retain_versions: ownPolicy?.retain_versions ?? null,
			interval: ownPolicy?.compact_interval_hours ?? null,
			target: ownPolicy?.target_rows_per_fragment ?? null,
			enabled: ownPolicy?.compact_enabled ?? true,
		};
		policyError = null;
		editingPolicy = true;
	}

	function policyFail(status: number, detailText: string): void {
		if (status === 401) policyError = 'Sign in to edit the maintenance policy.';
		else if (status === 403)
			policyError = 'Denied: a project policy needs project-admin access (can_administer).';
		else policyError = detailText;
	}

	async function savePolicy(): Promise<void> {
		if (busy) return;
		busy = true;
		policyError = null;
		const current = project;
		try {
			const res = await setProjectPolicy({ project: current, policy: policyRequestFrom(draft) });
			if (project !== current) return; // navigated away — drop the stale result
			if (res.ok) {
				editingPolicy = false;
				await loadPolicies();
			} else {
				policyFail(res.status, res.detail);
			}
		} catch (err) {
			if (project === current)
				policyError = `policy response drifted from the contract: ${String(err)}`;
		} finally {
			busy = false;
		}
	}

	async function removePolicy(): Promise<void> {
		if (busy) return;
		busy = true;
		policyError = null;
		const current = project;
		try {
			const res = await deleteProjectPolicy(current);
			if (project !== current) return;
			if (res.ok) await loadPolicies();
			else policyFail(res.status, res.detail);
		} catch (err) {
			if (project === current)
				policyError = `policy response drifted from the contract: ${String(err)}`;
		} finally {
			busy = false;
		}
	}

	$effect(() => {
		void project;
		detail = null;
		events = null;
		eventsDenied = false;
		lastStatus = 0;
		settled = false;
		// Reset every piece of policy state on a project change — INCLUDING the open editor, or a draft
		// opened on A would survive into B and Save would write A's knobs onto B's tenant.
		policies = null;
		policiesPart = 'loading';
		policiesDenied = null;
		policyError = null;
		editingPolicy = false;
		busy = false;
		load();
	});
</script>

<svelte:head><title>{project} · projects · rask</title></svelte:head>

<div class="page">
	<header>
		<FolderKanban size={15} />
		<h1 class="mono">{project}</h1>
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a
					href={resolve(`auth/login?redirect=${encodeURIComponent(page.url.pathname)}`)}>sign in</a
				> to view this project.
			</p>
		</div>
	{:else if missing}
		<!-- A project exists when its registry record does, not because a warehouse names it: one
		     created through POST /management/v1/projects may have no warehouse yet. The catalog answers
		     a reader who is neither an estate observer nor a member of the project exactly as it answers
		     a project that does not exist (404), so this says both, and tells the two apart for no one. -->
		<div class="empty" data-testid="project-not-visible">
			<p>
				No project <code class="mono">{project}</code> that you can see: it does not exist, or you hold
				no grant on it.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<!-- status 0 is an unreachable catalog (§1.0), where no server answered at all — printing
			     "HTTP 0" would name a status nothing sent. -->
			<p>Catalog unreachable{lastStatus === 0 ? '' : ` (HTTP ${lastStatus})`}.</p>
		</div>
	{:else if detail === null}
		<div class="empty"><p>Loading…</p></div>
	{:else}
		<!-- The catalog's lists, filtered to this project: the same estate-wide lists, narrowed in the
		     browser by `?project=` (a view, not a boundary). -->
		<nav class="catalog-links" aria-label="This project in the catalog">
			<a href={resolve(`catalog/warehouses?project=${encodeURIComponent(project)}`)}>Warehouses</a>
			<a href={resolve(`catalog/namespaces?project=${encodeURIComponent(project)}`)}>Namespaces</a>
			<a href={resolve(`catalog/tables?project=${encodeURIComponent(project)}`)}>Tables</a>
			<a href={resolve(`catalog/promotions?project=${encodeURIComponent(project)}`)}>Promotions</a>
		</nav>

		<!-- The hierarchy first: the relationship the estate is built on — project › warehouse ›
		     namespace › table — shown the moment the page opens. The view (`#lib/flow/hierarchy`) takes
		     the two rung reads as async props, so the catalog's gate is enforced against this user's
		     bearer. -->
		<section>
			<h2>Hierarchy</h2>
			<ProjectHierarchy
				{project}
				warehouses={detail.warehouses}
				fetchNamespaces={warehouseNamespaces}
				fetchTables={namespaceTables}
				hrefFor={rungHref}
			/>
		</section>
		<section>
			<h2>Warehouses</h2>
			{#if detail.warehouses.length === 0}
				<p class="mut">No warehouses provisioned for this project.</p>
			{:else}
				<table>
					<thead><tr><th>warehouse</th><th>bucket</th><th>serving</th><th>status</th></tr></thead>
					<tbody>
						{#each detail.warehouses as w (w.id)}
							<tr>
								<td>
									<!-- DOWN a rung: the warehouse's own catalog page. -->
									<a
										class="mono whlink"
										href={resolve('/catalog/warehouses/[id]', { id: encodeURIComponent(w.id) })}
										>{w.id}</a
									>
								</td>
								<td class="mono">{w.bucket}</td>
								<td class="mono">{w.serving ?? '—'}</td>
								<td><span class="chip mono" class:off={w.status !== 'active'}>{w.status}</span></td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>

		<section>
			<h2>Admins</h2>
			{#if detail.admins.length === 0}
				<p class="mut">(none listed — FGA off or unavailable)</p>
			{:else}
				<div class="refs">
					{#each detail.admins as a (a)}<Subject
							value={a}
							class="bg-muted border-success/45 rounded-sm border px-1.5"
						/>{/each}
				</div>
			{/if}
		</section>

		<!-- #71 what just happened HERE: the 10 most recent control events on this project and its
		     warehouses, newest first. The estate-wide, filterable stream is one reload-link away. -->
		<section>
			<h2>Recent activity</h2>
			{#if eventsDenied}
				<p class="mut">
					The event feed is estate-admin gated — ask an estate admin, or check
					<a href={resolve('/admin/events')}>the estate stream</a>.
				</p>
			{:else if events === null}
				<p class="mut">Events unavailable right now.</p>
			{:else if events.length === 0}
				<p class="mut">
					No governance changes on this project since the catalog started: its feed holds only
					those.
				</p>
			{:else}
				<ul class="evlist">
					{#each events as e (e.event_id)}
						<ControlEventLine event={e} />
					{/each}
				</ul>
				<p class="mut">
					<a href={resolve('/admin/events')}>All events, live and filterable →</a>
				</p>
			{/if}
		</section>

		<!-- #65 the tenant's maintenance plane: the project's own policy (the sweep's fallback for every
		     dataset no lower record claims) and a read-only view of every record that SHADOWS it. -->
		<section>
			<h2>Maintenance</h2>
			<p class="mut">
				A project policy is the sweep's fallback for this tenant's data. Resolution is
				<strong>winner-takes-all</strong>: a table record shadows a namespace record shadows this
				one, and the winning record supplies <em>every</em> field — a value it leaves unset falls to
				the sweep's global default, not to the record it shadowed. Tag-pinned versions (e.g.
				<code class="mono">blessed</code>) are never cleaned up, whatever any policy says.
			</p>

			{#if policiesPart === 'loading'}
				<p class="mut">Loading policies…</p>
			{:else if policiesPart === 'denied'}
				<p class="mut">{policiesDenied}</p>
			{:else if policiesPart === 'unavailable'}
				<p class="mut">
					Policies unavailable right now — not shown, to avoid an overwriting edit against a stale
					read.
				</p>
			{:else if policies}
				{#if policies.incomplete}
					<!-- A binding the catalog could not read is a namespace it could not see. Saying so is the
					     whole point: a quietly short list reads as "checked and clean". -->
					<p class="warn">
						<AlertTriangle size={13} />
						This list may be short — {policies.skipped_bindings.length} namespace binding record{policies
							.skipped_bindings.length === 1
							? ''
							: 's'} could not be read, so any policy under {policies.skipped_bindings.length === 1
							? 'that namespace'
							: 'those namespaces'} is missing from it.
					</p>
				{/if}

				{#if editingPolicy}
					<div class="policy-edit">
						<label
							>retention days <input
								class="mono"
								type="number"
								min="1"
								bind:value={draft.retention_days}
								placeholder="global default"
							/></label
						>
						<label
							>retain versions <input
								class="mono"
								type="number"
								min="1"
								bind:value={draft.retain_versions}
								placeholder="—"
							/></label
						>
						<label
							>compact every (h) <input
								class="mono"
								type="number"
								min="1"
								bind:value={draft.interval}
								placeholder="every sweep"
							/></label
						>
						<label
							>target rows/fragment <input
								class="mono"
								type="number"
								min="1024"
								bind:value={draft.target}
								placeholder="Lance default"
							/></label
						>
						<label class="check"
							><input type="checkbox" bind:checked={draft.enabled} /> maintenance enabled</label
						>
						<div class="btnrow">
							<Button size="sm" disabled={busy} onclick={savePolicy}>Save policy</Button>
							<Button variant="ghost" size="sm" onclick={() => (editingPolicy = false)}
								>Cancel</Button
							>
						</div>
						<p class="mut">
							An empty field means <em>inherit</em> — it is left out of the request, not sent as a cleared
							value, so the knobs this form has no control for (the compaction scan batch, auto-cleanup
							ownership) keep whatever the record already carries.
						</p>
					</div>
				{:else if ownPolicy}
					<div class="refs">
						{#if ownPolicy.retention_days}<span class="chip mono"
								>retention {ownPolicy.retention_days}d</span
							>{/if}
						{#if ownPolicy.retain_versions}<span class="chip mono"
								>keep last {ownPolicy.retain_versions}</span
							>{/if}
						{#if ownPolicy.compact_interval_hours}<span class="chip mono"
								>every {ownPolicy.compact_interval_hours}h</span
							>{/if}
						{#if ownPolicy.target_rows_per_fragment}<span class="chip mono">target {ownPolicy.target_rows_per_fragment}
						rows/frag</span>{/if}
						{#if !ownPolicy.compact_enabled}<span class="chip off mono">maintenance off</span>{/if}
						<Button variant="ghost" size="sm" onclick={startPolicyEdit}>Edit</Button>
						<Button variant="destructive" size="sm" disabled={busy} onclick={removePolicy}>
							<Trash2 size={12} /> Remove
						</Button>
					</div>
					{#if ownPolicy.buckets && ownPolicy.buckets.length > 0}
						<p class="mut">
							Covers <span class="mono">{ownPolicy.buckets.join(
								', ',
							)}</span> — the warehouse buckets
							resolved when the policy was set. A warehouse provisioned since is not covered until the
							policy is set again.
						</p>
					{/if}
				{:else}
					<p class="mut">
						No project policy — the sweep applies its global defaults to anything no table or
						namespace record claims.
						<Button variant="ghost" size="sm" onclick={startPolicyEdit}>Set policy</Button>
					</p>
				{/if}
				{#if policyError}<p class="error">{policyError}</p>{/if}

				<h3>Records inside this project</h3>
				{#if scopedPolicies.length === 0}
					<p class="mut">
						No table or namespace policy in this project{policies.namespaces.length === 0 && policies.buckets.length === 0
							? ' (it has no warehouses yet)'
							: ''}.
					</p>
				{:else}
					<table>
						<thead><tr><th>tier</th><th>object</th><th>retention</th><th>state</th></tr></thead>
						<tbody>
							{#each scopedPolicies as p (`${p.kind}:${p.id}`)}
								<tr>
									<td class="mono">{p.kind}</td>
									<td>
										<!-- DOWN a rung: the namespace's or table's own catalog page. -->
										<a
											class="mono whlink"
											href={p.kind === 'table'
												? resolve('/catalog/tables/[table]', { table: encodeURIComponent(p.id) })
												: resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(p.id) })}
											>{p.id}</a
										>
									</td>
									<td class="mono">{p.retention_days ? `${p.retention_days}d` : '—'}</td>
									<td>
										<span class="chip mono" class:off={!p.compact_enabled}
											>{p.compact_enabled ? 'on' : 'off'}</span
										>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				{/if}
			{/if}
		</section>

		<!-- Retiring the tenant. Last on the page and fenced off, because it is the one action here that
		     cannot be undone by repeating it. The catalog is the gate (project `can_administer`), so this
		     renders for anyone who can READ the project and the refusal is rendered honestly rather than
		     the affordance being guessed at from an identity this page does not hold. -->
		<section class="danger">
			<h2>Danger zone</h2>
			<p class="mut">
				Retiring <code class="mono">{project}</code> revokes every grant on it and drops its
				registry record. No bytes are touched: there is deliberately no cascade here, so its
				{detail.warehouses.length === 1 ? 'warehouse' : 'warehouses'} — and the buckets behind
				{detail.warehouses.length === 1 ? 'it' : 'them'} — have to be retired one rung at a time first.
			</p>
			<Button variant="destructive" size="sm" onclick={() => (deleting = true)}>
				<Trash2 size={14} /> Delete project
			</Button>
		</section>

		<ProjectDeleteDialog
			bind:open={deleting}
			{project}
			ondeleted={() => {
				// The project is gone — this very route's read would 404 on the next tick. Back to the
				// estate list, re-read from the server so the retired tenant is not still on it.
				goto(resolve('/projects'), { refreshAll: true });
			}}
		/>
	{/if}
</div>

<style>
	.evlist {
		list-style: none;
		margin: 0;
		padding: 0;
		font-size: 12px;
	}

	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 18px;
		color: var(--mut);
	}
	h1 {
		font-size: 20px;
		margin: 0;
		color: var(--ink);
	}
	.catalog-links {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		margin: -4px 0 18px;
		font-size: 13px;
	}
	.catalog-links a {
		color: var(--muted-foreground);
		text-decoration: underline;
		text-underline-offset: 4px;
	}
	.catalog-links a:hover {
		color: var(--foreground);
	}
	section {
		margin-bottom: 26px;
	}
	h2 {
		font-size: 14px;
		margin: 0 0 8px;
	}
	h3 {
		font-size: 12px;
		margin: 18px 0 6px;
		color: var(--mut);
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
		margin: 4px 0;
	}
	.mut :global(button) {
		margin-left: 6px;
		vertical-align: middle;
	}
	/* The short-list banner: NOT a .mut. A list that may be missing records has to read differently
	   from prose, or it is exactly the swallowed degrade the `incomplete` flag exists to prevent. */
	.warn {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		color: var(--amber, #d18b28);
		border: 1px solid color-mix(in srgb, var(--amber, #d18b28) 45%, var(--line));
		border-radius: var(--radius-sm);
		padding: 6px 9px;
		margin: 8px 0;
	}
	.error {
		font-size: 12px;
		color: var(--fail);
		margin: 6px 0 0;
	}
	.policy-edit {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 10px 16px;
		font-size: 12px;
		color: var(--mut);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 12px;
	}
	.policy-edit input[type='number'] {
		width: 8ch;
		margin-left: 6px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 2px 6px;
	}
	.policy-edit .check {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.policy-edit .btnrow {
		display: flex;
		gap: 8px;
	}
	.policy-edit .mut {
		flex-basis: 100%;
		max-width: 62ch;
	}
	table {
		border-collapse: collapse;
		font-size: 12px;
		width: 100%;
	}
	th {
		text-align: left;
		color: var(--faint);
		font-weight: 500;
		padding: 3px 14px 3px 0;
		border-bottom: 1px solid var(--line);
	}
	td {
		padding: 5px 14px 5px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
	}
	.whlink {
		color: var(--ink);
		text-decoration: none;
	}
	.whlink:hover {
		text-decoration: underline;
	}
	.chip {
		background: var(--panel-2);
		border: 1px solid color-mix(in srgb, var(--ok) 45%, var(--line));
		border-radius: var(--radius-sm);
		padding: 0 7px;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--amber, #d18b28) 55%, var(--line));
	}
	.refs {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 32px 0;
	}
	.danger {
		border-top: 1px solid var(--line);
		padding-top: 16px;
	}
	.danger h2 {
		color: var(--fail);
	}
	.danger p {
		max-width: 62ch;
		margin-bottom: 10px;
	}
</style>
