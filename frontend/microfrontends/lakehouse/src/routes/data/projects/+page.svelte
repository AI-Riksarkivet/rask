<script lang="ts">
	// `/projects` — the hierarchy root (goal cond 3): project → warehouse → namespace → table.
	// An estate observer gets the full tenant list from /v1/projects; a plain member gets 403 there
	// and falls back to their own memberships from the frozen /v1/me contract — degrade, never 500.
	// Estate admins (per /v1/me) additionally get the project-creation flow (goal cond 6).
	import { FolderKanban, Plus, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { fetchProjects, type ProjectSummary } from '$lib/data/catalog';
	import type { Me } from '@rask/api';
	import { fetchMeViaBff } from '$lib/http';
	import ProjectCreateDialog from '$lib/data/ProjectCreateDialog.svelte';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	type Row = { project: string; role: 'admin' | 'member' | null; warehouses: number | null };

	let rows = $state<Row[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let estateWide = $state(false);
	let me = $state<Me | null>(null);
	let creating = $state(false);

	const unauthorized = $derived(rows === null && settled && lastStatus === 401);
	const offline = $derived(rows === null && settled && ![200, 401, 403].includes(lastStatus));
	// The initial-admin prefill in FGA subject form (`user:<sub>`; an already-typed sub passes through).
	const meSubject = $derived(me === null ? '' : me.sub.includes(':') ? me.sub : `user:${me.sub}`);

	async function load(): Promise<void> {
		const [estate, meRes] = await Promise.all([fetchProjects(), fetchMeViaBff()]);
		settled = true;
		me = meRes;
		const roleOf = new Map((meRes?.projects ?? []).map((p) => [p.project, p.role]));
		if (estate.ok) {
			estateWide = true;
			rows = estate.data.map(
				(p: ProjectSummary): Row => ({
					project: p.project,
					role: roleOf.get(p.project) ?? null,
					warehouses: p.warehouses.length,
				}),
			);
			lastStatus = 200;
		} else if (estate.status === 403 && meRes) {
			// Not an estate observer — the caller's OWN memberships are the honest gallery.
			estateWide = false;
			rows = meRes.projects.map(
				(p): Row => ({
					project: p.project,
					role: p.role,
					warehouses: null,
				}),
			);
			lastStatus = 200;
		} else {
			lastStatus = estate.status;
		}
	}

	$effect(() => {
		load();
	});
</script>

<svelte:head><title>Projects · lance</title></svelte:head>

<div class="page">
	<header>
		<FolderKanban size={16} />
		<h1>Projects</h1>
		<span class="sub mono"
			>{estateWide ? 'every tenant in the estate' : 'your memberships'} · project → warehouse → namespace
			→ table</span
		>
		{#if me?.estate_admin}
			<button class="new" onclick={() => (creating = true)}>
				<Plus size={12} /> New project
			</button>
		{/if}
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to browse projects.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}).</p>
		</div>
	{:else if rows === null}
		<div class="empty"><p>Loading…</p></div>
	{:else if rows.length === 0}
		<div class="empty">
			<p>No projects visible — ask a project admin for access, or provision a warehouse.</p>
		</div>
	{:else}
		<ul class="list">
			{#each rows as p (p.project)}
				<li>
					<a class="row" href={`${base}/data/projects/${encodeURIComponent(p.project)}`}>
						<span class="mono name">{p.project}</span>
						{#if p.role}<span class="chip mono">{p.role}</span>{/if}
						{#if p.warehouses !== null}
							<span class="count">{p.warehouses} warehouse{p.warehouses === 1 ? '' : 's'}</span>
						{/if}
					</a>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<!-- Goal cond 6 — visible only to estate admins (the /v1/me gate); the gallery reflects the new
     project on the reload `oncreated` triggers. -->
<ProjectCreateDialog bind:open={creating} defaultAdmin={meSubject} oncreated={load} />

<style>
	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-bottom: 18px;
	}
	h1 {
		font-size: 20px;
		margin: 0;
	}
	.sub {
		color: var(--faint);
		font-size: 12px;
	}
	.new {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		margin-left: auto;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.new:hover {
		border-color: var(--mut);
	}
	.list {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 10px;
		border-bottom: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
		color: var(--ink);
		text-decoration: none;
		font-size: 13px;
	}
	.row:hover {
		background: var(--panel-2);
	}
	.name {
		font-weight: 600;
	}
	.chip {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: 999px;
		color: var(--mut);
		font-size: 11px;
		padding: 0 8px;
	}
	.count {
		margin-left: auto;
		color: var(--faint);
		font-size: 12px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 32px 0;
	}
</style>
