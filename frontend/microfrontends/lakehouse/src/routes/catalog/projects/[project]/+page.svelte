<script lang="ts">
	// `/projects/<p>` — the project rung of the hierarchy (goal cond 3): the tenant's warehouses
	// (from the first-class projects API through the /capi pass-through) linking down into the
	// warehouse page, plus its effective admins. Gated by the catalog; degrade states are honest.
	import { FolderKanban, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { fetchProject, type ProjectSummary } from '$lib/data/catalog';

	const project = $derived(page.params.project ?? '');

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
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
		<a class="back" href={`${base}/data/projects`}>Projects</a>
		<span class="sep">/</span>
		<FolderKanban size={15} />
		<h1 class="mono">{project}</h1>
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to view this project.
			</p>
		</div>
	{:else if denied}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>You don't have access to this project's registry facts.</p>
		</div>
	{:else if missing}
		<div class="empty"><p>No such project — no warehouse claims it.</p></div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}).</p>
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
									<a class="mono whlink" href={`${base}/data/warehouses/${encodeURIComponent(w.id)}`}
										>{w.id}</a
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
