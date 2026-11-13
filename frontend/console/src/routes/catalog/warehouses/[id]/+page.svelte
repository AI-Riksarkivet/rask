<script lang="ts">
	// `/warehouses/<id>` — the warehouse rung of the hierarchy (goal cond 3): the registry record
	// (project, bucket, status, root) + the namespaces directly under it, from the spec's
	// ListNamespaces on the warehouse's own name — the same listing the delete door reads for what the
	// warehouse still holds, so an empty namespace is listed here as it is refused there (#66). The
	// table registry only ENRICHES each namespace with its table count.
	//
	// The estate's own warehouse has no registry record: its record read is a 404 while the root
	// listing names it, and the page says what it is rather than that it is missing.
	import { RefreshCw, ShieldAlert, Warehouse as WarehouseIcon } from '@lucide/svelte';
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import type { Warehouse } from '#lib/data/catalog.ts';
	import { fetchTables } from '#lib/data/remote/catalog.api.ts';
	import {
		fetchServedWarehouses,
		fetchWarehouse,
		fetchWarehouseNamespaces,
	} from '#lib/data/remote/warehouses.api.ts';
	import DeclaredInGit, { declaredInGit } from '#lib/data/DeclaredInGit.svelte';
	import { namespaceOfTable, stageOf } from '#lib/data/stage.ts';
	import StageBadge from '#lib/data/StageBadge.svelte';

	const id = $derived(page.params.id ?? '');

	let wh = $state<Warehouse | null>(null);
	let tables = $state<string[] | null>(null);
	// The warehouse's listing. `null` = the read itself failed (rendered as its own honest state, never
	// silently collapsed into "no namespaces" — that collapse was the #66 bug's shape).
	let bound = $state<string[] | null>(null);
	/** The root listing names this id and the registry holds no record of it: the estate's own. */
	let estate = $state(false);
	let lastStatus = $state(0);
	let settled = $state(false);

	const unauthorized = $derived(wh === null && settled && lastStatus === 401);
	const denied = $derived(wh === null && settled && lastStatus === 403);
	const missing = $derived(wh === null && settled && lastStatus === 404 && !estate);
	const offline = $derived(wh === null && settled && ![200, 401, 403, 404].includes(lastStatus));

	async function load(): Promise<void> {
		const current = id;
		const [whRes, nsRes, tRes, served] = await Promise.all([
			fetchWarehouse(current),
			fetchWarehouseNamespaces(current),
			fetchTables(),
			fetchServedWarehouses(),
		]);
		if (id !== current) return; // latest-wins across navigation
		settled = true;
		if (whRes.ok) {
			wh = whRes.data;
			lastStatus = 200;
		} else {
			wh = null;
			lastStatus = whRes.status;
		}
		estate =
			!whRes.ok && whRes.status === 404 && served.ok && served.data.namespaces.includes(current);
		bound = nsRes.ok ? [...nsRes.data.namespaces].sort() : null;
		tables = tRes.ok ? [...tRes.data.tables].sort() : null;
	}

	$effect(() => {
		void id;
		wh = null;
		tables = null;
		bound = null;
		estate = false;
		lastStatus = 0;
		settled = false;
		load();
	});

	// The warehouse's namespaces: the LIST is the warehouse's own listing, verbatim — including a
	// namespace with zero tables, which is exactly what the delete door will refuse on. The table
	// registry only decorates each namespace with its table count (0 when the table read failed or
	// genuinely holds none — the count is enrichment, so its absence never hides a row).
	const namespaces = $derived.by(() => {
		if (bound === null) return [];
		const counts = new Map<string, number>();
		for (const t of tables ?? []) {
			const ns = namespaceOfTable(t);
			counts.set(ns, (counts.get(ns) ?? 0) + 1);
		}
		return bound.map((ns) => ({ ns, count: counts.get(ns) ?? 0, info: stageOf(ns) }));
	});

	function statusOf(w: Warehouse): string {
		return w.status ?? 'active';
	}
</script>

{#snippet namespaceList()}
	<section>
		<h2>Namespaces</h2>
		<p class="mut">
			Directly under this warehouse, as the catalog lists them — the same listing the delete door
			reads.
		</p>
		{#if bound === null}
			<p class="mut">
				This warehouse’s namespaces could not be listed right now, so the list is not shown.
			</p>
		{:else if namespaces.length === 0}
			<p class="mut">No namespaces in this warehouse that you can see.</p>
		{:else}
			<ul class="list">
				{#each namespaces as { ns, count, info } (ns)}
					<li>
						<a
							class="row"
							href={resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(ns) })}
						>
							<span class="mono">{ns}</span>
							{#if info}<StageBadge {info} />{/if}
							<span class="count">{count} table{count === 1 ? '' : 's'}</span>
						</a>
					</li>
				{/each}
			</ul>
		{/if}
	</section>
{/snippet}

<svelte:head><title>{id} · warehouses · rask</title></svelte:head>

<div class="page">
	<header>
		<WarehouseIcon size={15} />
		<h1 class="mono">{id}</h1>
		{#if wh}
			<span class="chip mono" class:off={statusOf(wh) !== 'active'}>{statusOf(wh)}</span>
			{#if declaredInGit(wh)}<DeclaredInGit />{/if}
		{/if}
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a
					href={resolve(`auth/login?redirect=${encodeURIComponent(page.url.pathname)}`)}>sign in</a
				> to view this warehouse.
			</p>
		</div>
	{:else if denied || missing}
		<!-- A 403 is the catalog keeping existence to itself, so a 404 reads the same: the wording never
		     tells a missing warehouse from a hidden one. -->
		<div class="empty" data-testid="warehouse-not-visible">
			<p>No warehouse {id} that you can see: it does not exist, or you hold no grant on it.</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}).</p>
		</div>
	{:else if estate}
		<section>
			<h2>The estate’s own warehouse</h2>
			<p class="mut" data-testid="estate-warehouse">
				It holds the lakehouse’s own namespaces (its provenance) and belongs to no project. It has
				no registry record, so it has no bucket, status or lifecycle of its own, and it cannot be
				deactivated or deleted.
			</p>
		</section>
		{@render namespaceList()}
	{:else if wh === null}
		<div class="empty"><p>Loading…</p></div>
	{:else}
		<section>
			<h2>Registry record</h2>
			<div class="facts mono">
				<!-- The rung ABOVE this one: a project is the top of the hierarchy and its page is
				     `/projects/<p>` (2026-08-03 ruling). -->
				<span
					>project <a
						class="plink"
						href={resolve('/projects/[project]', { project: encodeURIComponent(wh.project) })}
						>{wh.project}</a
					></span
				>
				<span>bucket {wh.bucket}</span>
				<span class="loc">{wh.root_uri}</span>
			</div>
		</section>

		{@render namespaceList()}
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
	.facts {
		display: flex;
		flex-wrap: wrap;
		gap: 14px;
		font-size: 12px;
		color: var(--mut);
	}
	.loc {
		color: var(--faint);
	}
	.plink {
		color: var(--ink);
		text-decoration: none;
	}
	.plink:hover {
		text-decoration: underline;
	}
	.chip {
		background: var(--panel-2);
		border: 1px solid color-mix(in srgb, var(--ok) 45%, var(--line));
		border-radius: var(--radius-sm);
		font-size: 11px;
		padding: 0 7px;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--amber, #d18b28) 55%, var(--line));
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
		padding: 7px 10px;
		border-bottom: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
		color: var(--ink);
		text-decoration: none;
		font-size: 13px;
	}
	.row:hover {
		background: var(--panel-2);
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
