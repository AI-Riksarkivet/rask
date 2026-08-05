<script lang="ts">
	// `/projects/<p>` — ONE project: its metadata and overview (the warehouses claiming it, its
	// effective admins). Top of the hierarchy by the 2026-08-03 ruling — project › warehouse ›
	// namespace › table — so the page describing one project is a top-level page in the main menu,
	// and the lakehouse keeps every rung BELOW it. The drill-down therefore crosses zones exactly
	// once, at the project→warehouse rung.
	//
	// Gated by the catalog; every degrade state is named honestly rather than collapsed into "empty".
	import { FolderKanban, RefreshCw, ShieldAlert, Trash2 } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import type { ProjectSummary } from '$lib/catalog';
	import ProjectDeleteDialog from '$lib/ProjectDeleteDialog.svelte';
	import ProjectHierarchy from '$lib/ProjectHierarchy.svelte';
	import { fetchProject, projectEvents, type ProjectEvent } from '$lib/remote/warehouses.remote';

	const project = $derived(page.params.project ?? '');

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, navbar-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let detail = $state<ProjectSummary | null>(null);
	// #71 the project's 10 most recent control events. `denied` is its own quiet state — the events
	// feed is estate-admin gated at the catalog, and a project admin who is not estate admin should
	// see a page that simply omits the panel's content, not an error.
	let events = $state<ProjectEvent[] | null>(null);
	let eventsDenied = $state(false);
	let lastStatus = $state(0);
	let settled = $state(false);
	let deleting = $state(false);

	const unauthorized = $derived(detail === null && settled && lastStatus === 401);
	const denied = $derived(detail === null && settled && lastStatus === 403);
	const missing = $derived(detail === null && settled && lastStatus === 404);
	const offline = $derived(
		detail === null && settled && ![200, 401, 403, 404].includes(lastStatus),
	);

	async function load(): Promise<void> {
		const current = project;
		const [res, evRes] = await Promise.all([fetchProject(current), projectEvents(current)]);
		if (project !== current) return; // latest-wins across navigation
		settled = true;
		if (res.ok) {
			detail = res.data;
			lastStatus = 200;
		} else {
			detail = null;
			lastStatus = res.status;
		}
		events = evRes.ok ? evRes.data : null;
		eventsDenied = !evRes.ok && (evRes.status === 401 || evRes.status === 403);
	}

	$effect(() => {
		void project;
		detail = null;
		events = null;
		eventsDenied = false;
		lastStatus = 0;
		settled = false;
		load();
	});
</script>

<svelte:head><title>{project} · projects · lance</title></svelte:head>

<div class="page">
	<header>
		<a class="back" href="/projects">Projects</a>
		<span class="sep">/</span>
		<FolderKanban size={15} />
		<h1 class="mono">{project}</h1>
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref}>sign in</a> to view this project.
			</p>
		</div>
	{:else if denied}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>You don't have access to this project's registry facts.</p>
		</div>
	{:else if missing}
		<!-- Existence is the project REGISTRY record, not a warehouse implying one: a tenant created
		     through POST /v1/projects legitimately has zero warehouses and must not read as missing.
		     The old copy ("no warehouse claims it") stated the pre-registry definition, so an FGA-only
		     ghost — a project you hold a role on that the catalog never recorded — showed this text
		     while the gallery listed it, naming neither the cause nor the fix. -->
		<div class="empty">
			<p>
				No such project — the catalog has no registry record for <code class="mono">{project}</code>.
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
		<!-- The hierarchy FIRST (#104's ruling): the relationship the estate is built on —
		     project › warehouse › namespace › table — shown the moment the page opens. -->
		<section>
			<h2>Hierarchy</h2>
			<ProjectHierarchy {project} warehouses={detail.warehouses} />
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
									<!-- DOWN a rung and ACROSS the zone seam: warehouses belong to the lakehouse.
									     data-sveltekit-reload is mandatory — a soft nav would resolve against this
									     zone's route manifest, which owns no /lakehouse route, and 404. -->
									<a
										class="mono whlink"
										href={`/lakehouse/catalog/warehouses/${encodeURIComponent(w.id)}`}
										data-sveltekit-reload>{w.id}</a
									>
								</td>
								<td class="mono">{w.bucket}</td>
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
					{#each detail.admins as a (a)}<span class="chip mono">{a}</span>{/each}
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
					<a href="/lakehouse/admin/events" data-sveltekit-reload>the estate stream</a>.
				</p>
			{:else if events === null}
				<p class="mut">Events unavailable right now.</p>
			{:else if events.length === 0}
				<p class="mut">No governance changes recorded on this project yet.</p>
			{:else}
				<ul class="evlist">
					{#each events as e (e.event_id)}
						<li class="mono">
							<span class="evaction">{e.action.replaceAll('_', ' ')}</span>
							<span class="evobj">{e.object_id}</span>
							<span class="evts">{e.occurred_at.slice(0, 19).replace('T', ' ')}</span>
						</li>
					{/each}
				</ul>
				<p class="mut">
					<a href="/lakehouse/admin/events" data-sveltekit-reload>All events, live and filterable →</a>
				</p>
			{/if}
		</section>

		<!-- Retiring the tenant. Last on the page and fenced off, because it is the one action here that
		     cannot be undone by repeating it. The catalog is the gate (project `can_administer`), so this
		     renders for anyone who can READ the project and the refusal is rendered honestly rather than
		     the affordance being guessed at from an identity this page does not hold. -->
		<section class="danger">
			<h2>Danger zone</h2>
			<p class="mut">
				Retiring <code class="mono">{project}</code> revokes every grant on it and drops its registry
				record. No bytes are touched: there is deliberately no cascade here, so its
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
	goto('/projects', { invalidateAll: true });
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
	.evlist li {
		display: grid;
		grid-template-columns: 180px 1fr auto;
		gap: 12px;
		align-items: baseline;
		padding: 4px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
	}
	.evaction {
		color: var(--fg);
	}
	.evobj {
		color: var(--mut);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.evts {
		color: var(--faint);
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
	.back {
		color: var(--mut);
		font-size: 13px;
		text-decoration: none;
	}
	.back:hover {
		color: var(--ink);
	}
	.sep {
		color: var(--faint);
	}
	section {
		margin-bottom: 26px;
	}
	h2 {
		font-size: 14px;
		margin: 0 0 8px;
	}
	.mut {
		color: var(--faint);
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
