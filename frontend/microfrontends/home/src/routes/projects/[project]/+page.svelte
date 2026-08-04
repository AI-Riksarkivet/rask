<script lang="ts">
	// `/projects/<p>` — ONE project: its metadata and overview (the warehouses claiming it, its
	// effective admins). Top of the hierarchy by the 2026-08-03 ruling — project › warehouse ›
	// namespace › table — so the page describing one project is a top-level page in the main menu,
	// and the lakehouse keeps every rung BELOW it. The drill-down therefore crosses zones exactly
	// once, at the project→warehouse rung.
	//
	// Gated by the catalog; every degrade state is named honestly rather than collapsed into "empty".
	import { FolderKanban, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { page } from '$app/state';
	import type { ProjectSummary } from '$lib/catalog';
	import { fetchProject } from '$lib/remote/warehouses.remote';

	const project = $derived(page.params.project ?? '');

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, navbar-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let detail = $state<ProjectSummary | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);

	const unauthorized = $derived(detail === null && settled && lastStatus === 401);
	const denied = $derived(detail === null && settled && lastStatus === 403);
	const missing = $derived(detail === null && settled && lastStatus === 404);
	const offline = $derived(
		detail === null && settled && ![200, 401, 403, 404].includes(lastStatus),
	);

	async function load(): Promise<void> {
		const current = project;
		const res = await fetchProject(current);
		if (project !== current) return; // latest-wins across navigation
		settled = true;
		if (res.ok) {
			detail = res.data;
			lastStatus = 200;
		} else {
			detail = null;
			lastStatus = res.status;
		}
	}

	$effect(() => {
		void project;
		detail = null;
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
			<p>No such project — the catalog has no registry record for <code class="mono">{project}</code>.</p>
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
</style>
