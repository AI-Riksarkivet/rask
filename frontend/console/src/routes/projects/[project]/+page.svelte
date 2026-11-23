<script lang="ts">
	import { resolve } from '$app/paths';
	// `/projects/<p>` — ONE project: its metadata and overview (the warehouses claiming it, its
	// effective admins). A project is the top of the hierarchy — project › warehouse › namespace ›
	// table — so the page describing one is a top-level page in the main menu, and every rung BELOW it
	// links to its catalog page.
	//
	// Gated by the catalog; every degrade state is named honestly rather than collapsed into "empty".
	import { FolderKanban, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import * as v from 'valibot';
	import { ProjectHierarchy, type HierarchyHref } from '#lib/flow/hierarchy/index.ts';
	import { page } from '$app/state';
	import ProjectActivity from '#lib/ProjectActivity.svelte';
	import ProjectDangerZone from '#lib/ProjectDangerZone.svelte';
	import ProjectMaintenance from '#lib/ProjectMaintenance.svelte';
	import { Subject } from '#lib/access/identity/index.ts';
	import { isEstateAdmin } from '#lib/estate-admin.ts';
	import { fetchProjectPolicies } from '#lib/remote/policies.api.ts';
	import { fetchProject, namespaceTables, projectEvents } from '#lib/remote/warehouses.api.ts';
	import { fetchWarehouseNamespaces } from '#lib/data/remote/warehouses.api.ts';
	import ProjectLabels from '#lib/data/ProjectLabels.svelte';
	import { GrantsPanel, type GrantsClient } from '#lib/access/grants-panel/index.ts';
	import {
		fetchAccess,
		fetchMyPermissions,
		grantAccess,
		revokeAccess,
	} from '#lib/data/remote/access-objects.api.ts';
	import DeclaredInGit, { declaredInGit } from '#lib/data/DeclaredInGit.svelte';

	const project = $derived(page.params.project ?? '');

	// A project has the list, grant, revoke and self-view doors, and no check door, so the panel
	// offers no simulator here.
	const grantsClient: GrantsClient = {
		fetchAccess: (kind, id) => fetchAccess({ kind, id }),
		grantAccess: (kind, id, user, relation) => grantAccess({ kind, id, user, relation }),
		revokeAccess: (kind, id, user, relation) => revokeAccess({ kind, id, user, relation }),
		fetchMyPermissions: (kind, id) => fetchMyPermissions({ kind, id }),
	};

	// The graph is a map: every rung below the project links to the catalog page that owns it.
	// `project` returns null: from `/projects/<id>`, the project rung IS this page.
	const rungHref: HierarchyHref = (rung) => {
		if (rung.level === 'project') return null;
		if (rung.level === 'warehouse')
			return resolve('/catalog/warehouses/[id]', { id: encodeURIComponent(rung.id) });
		if (rung.level === 'namespace')
			return resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(rung.id) });
		return resolve('/catalog/tables/[table]', { table: encodeURIComponent(rung.id) });
	};

	// The project's record first; its policies and events only once there is a project to read them for,
	// since a project the catalog does not show this caller has neither. Each read is one instance per
	// project, so a navigation reads the new project's with no latest-wins guard to hand-write.
	const projectRead = $derived(fetchProject(project));
	const answer = $derived(projectRead.current);
	const detail = $derived(answer?.ok ? answer.data : null);
	const lastStatus = $derived(answer === undefined ? 0 : answer.ok ? 200 : answer.status);
	const settled = $derived(answer !== undefined);

	const unauthorized = $derived(detail === null && settled && lastStatus === 401);
	// A 403 and a 404 read the same: the catalog keeps a project's existence to itself.
	const missing = $derived(detail === null && settled && [403, 404].includes(lastStatus));
	const offline = $derived(
		detail === null && settled && ![200, 401, 403, 404].includes(lastStatus),
	);

	// The events feed is estate-wide and estate-admin gated at the catalog, so it is read only for an
	// estate admin: anyone else would be refused on every visit, and sees the panel's quiet denial.
	const estateAdmin = $derived(isEstateAdmin(page.data.me));
	// By value, so a re-read of the same record issues nothing new.
	const visible = $derived(detail !== null);
	const warehouseIds = $derived(JSON.stringify(detail?.warehouses.map((w) => w.id) ?? []));
	/** The ids back from their key, narrowed rather than asserted. */
	const WAREHOUSE_IDS = v.array(v.string());
	// Issued together, policies first, once the project is there to read them for.
	const reads = $derived(
		visible
			? {
					policies: fetchProjectPolicies(project),
					events: estateAdmin
						? projectEvents({
								project,
								warehouses: v.parse(WAREHOUSE_IDS, JSON.parse(warehouseIds)),
							})
						: null,
				}
			: null,
	);
</script>

<svelte:head><title>{project} · projects · rask</title></svelte:head>

<div class="page">
	<header>
		<FolderKanban size={15} />
		<h1 class="mono">{project}</h1>
		{#if declaredInGit(detail)}<DeclaredInGit />{/if}
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
	{:else if detail === null || reads === null}
		<div class="empty"><p>Loading…</p></div>
	{:else}
		<!-- The catalog's lists, filtered to this project: the same estate-wide lists, narrowed in the
		     browser by `?project=` (a view, not a boundary). -->
		<nav class="catalog-links" aria-label="This project in the catalog">
			<a href={resolve(`catalog/warehouses?project=${encodeURIComponent(project)}`)}>Warehouses</a>
			<a href={resolve(`catalog/namespaces?project=${encodeURIComponent(project)}`)}>Namespaces</a>
			<a href={resolve(`catalog/tables?project=${encodeURIComponent(project)}`)}>Tables</a>
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
				fetchNamespaces={fetchWarehouseNamespaces}
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
					<thead><tr><th>warehouse</th><th>bucket</th><th>status</th></tr></thead>
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
								<td
									><span class={['chip mono', { off: w.status !== 'active' }]}>{w.status}</span></td
								>
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

		<!-- Who holds the project's own rungs: `describer` and `modifier` reach every warehouse of the
		     project, `decider` lets a service ask the decision door about its objects. Membership is the
		     members door's, not this one's. -->
		<section>
			<h2>Access</h2>
			<GrantsPanel dataset={project} kind="project" client={grantsClient} />
		</section>

		<!-- The labels this project defines, which its objects' owners attach (ADR 0211). Keyed by the
		     project, so a form opened on one project cannot carry its draft into the next. -->
		{#key project}
			<ProjectLabels {project} />
		{/key}

		<!-- #71 what just happened HERE; the read is the page's, issued with the policies. -->
		<ProjectActivity read={reads.events} />

		<!-- #65 the tenant's maintenance plane, keyed by the project so a draft never crosses tenants. -->
		{#key project}
			<ProjectMaintenance {project} read={reads.policies} />
		{/key}

		<ProjectDangerZone
			{project}
			warehouses={detail.warehouses.length}
			declared={declaredInGit(detail)}
		/>
	{/if}
</div>

<style>
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
		color: var(--muted-foreground);
	}
	h1 {
		font-size: var(--text-xl);
		line-height: var(--text-xl--line-height);
		letter-spacing: var(--text-xl--letter-spacing);
		margin: 0;
		color: var(--foreground);
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
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
		margin: 4px 0;
	}
	table {
		border-collapse: collapse;
		font-size: 12px;
		width: 100%;
	}
	th {
		text-align: left;
		color: var(--muted-foreground);
		font-weight: 500;
		padding: 3px 14px 3px 0;
		border-bottom: 1px solid var(--border);
	}
	td {
		padding: 5px 14px 5px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
	}
	.whlink {
		color: var(--foreground);
		text-decoration: none;
	}
	.whlink:hover {
		text-decoration: underline;
	}
	.chip {
		background: var(--muted);
		border: 1px solid color-mix(in srgb, var(--success) 45%, var(--border));
		border-radius: var(--radius-sm);
		padding: 0 7px;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--warning) 55%, var(--border));
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
		color: var(--muted-foreground);
		padding: 32px 0;
	}
</style>
