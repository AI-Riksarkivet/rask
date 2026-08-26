<script lang="ts">
	// Per-dataset detail (Marquez-parity): facets (creator / schema time-travel / governance /
	// grants / read-audit), the direct upstream + downstream neighbors, and the latest producing
	// runs with their reproducibility pins. Everything is keyed by the route's dataset name, so
	// navigating between datasets never bleeds state.
	import { ArrowLeft, Columns3, Network } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { enter } from '@rask/ui/motion';
	import { GrantsPanel, type GrantsClient } from '@rask/ui/grants-panel';
	import DatasetProvenance from '$lib/lineage/DatasetProvenance.svelte';
	import GovernancePanel from '$lib/lineage/GovernancePanel.svelte';
	import ReadersPanel from '$lib/ReadersPanel.svelte';
	import RunInputs from '$lib/lineage/RunInputs.svelte';
	import { fetchDownstream, fetchProducers, fetchUpstream } from '$lib/api';
	import {
		checkAccess,
		fetchAccess,
		fetchMyPermissions,
		grantAccess,
		revokeAccess,
	} from '$lib/data/remote/access-objects.remote';
	import type { DatasetRef, ProducerInfo } from '@rask/api/lineage';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	const name = $derived(decodeURIComponent(page.params.name ?? ''));

	// The zone-owned catalog seam the shared @rask/ui GrantsPanel calls (the lib never owns an API
	// client). The panel's positional signature is bound to the remote functions' single argument here.
	const grantsClient: GrantsClient = {
		fetchAccess: (kind, id) => fetchAccess({ kind, id }),
		checkAccess: (kind, id, user, relation) => checkAccess({ kind, id, user, relation }),
		grantAccess: (kind, id, user, relation) => grantAccess({ kind, id, user, relation }),
		revokeAccess: (kind, id, user, relation) => revokeAccess({ kind, id, user, relation }),
		// #143: the panel renders a refused Grant/Revoke DISABLED WITH ITS REASON rather than letting
		// the user discover the denial from a 403. It needs the caller's own verdicts to do that, and
		// the zone owns the transport — so the seam gains a fifth member like the four above.
		fetchMyPermissions: (kind, id) => fetchMyPermissions({ kind, id }),
	};

	// All three reads are keyed by the dataset they were fetched FOR (latest-wins by derivation —
	// a stale response for a clicked-away dataset never lands).
	let producerState = $state<{ for: string; runs: ProducerInfo[] } | null>(null);
	let neighborState = $state<{
		for: string;
		upstream: DatasetRef[];
		downstream: DatasetRef[];
	} | null>(null);

	const producers = $derived(producerState?.for === name ? producerState.runs : []);
	const upstream = $derived(neighborState?.for === name ? neighborState.upstream : []);
	const downstream = $derived(neighborState?.for === name ? neighborState.downstream : []);

	// The distinct Lance versions this dataset was written at (from its producing runs) — the
	// version options the schema-time-travel viewer steps through.
	const versions = $derived([
		...new Set(producers.map((r) => r.dataset_version).filter((v): v is string => !!v)),
	]);

	async function load(current: string): Promise<void> {
		const [prod, up, down] = await Promise.all([
			fetchProducers(current),
			fetchUpstream(current),
			fetchDownstream(current),
		]);
		if (name !== current) return; // latest-wins
		if (prod) producerState = { for: current, runs: prod.producers ?? [] };
		if (up || down) {
			neighborState = {
				for: current,
				upstream: up?.related ?? (neighborState?.for === current ? neighborState.upstream : []),
				downstream:
					down?.related ?? (neighborState?.for === current ? neighborState.downstream : []),
			};
		}
	}

	// Keyed live read: the dataset name re-reads immediately on navigation, the lineage cursor re-reads
	// when that dataset actually gains a run / neighbour. No timer, so an unchanged dataset is read once.
	liveRead(
		lineageTick,
		(current: string) => load(current),
		() => name,
	);

	const stateColor = (s?: string | null) =>
		/FAIL|ABORT/i.test(s ?? '') ? 'var(--fail)' : s === 'COMPLETE' ? 'var(--ok)' : 'var(--mut)';

	function fmtBytes(n: number | null | undefined): string {
		if (n == null) return '';
		const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
		let v = n;
		let u = 0;
		while (v >= 1024 && u < units.length - 1) {
			v /= 1024;
			u += 1;
		}
		return `${v.toFixed(v >= 10 || u === 0 ? 0 : 1)} ${units[u]}`;
	}
</script>

<svelte:head><title>{name} · lineage · rask</title></svelte:head>

<div class="page">
	<a class="back" href="{base}/lineage/datasets"><ArrowLeft size={12} /> datasets</a>
	<header>
		<h1 class="mono">{name}</h1>
		<a class="viewlink" href={`${base}/lineage/columns?dataset=${encodeURIComponent(name)}`}>
			<Columns3 size={12} /> column lineage
		</a>
		<!-- Carries the dataset, so the graph opens ROOTED on it rather than on the whole estate —
		     the same query-param idiom as the columns link directly above. -->
		<a
			class="viewlink"
			href={`${base}/lineage?node=${encodeURIComponent(name)}&kind=dataset&depth=1`}
		>
			<Network size={12} /> graph
		</a>
	</header>

	<div class="grid">
		<section class="facets" {@attach enter({ y: 6 })}>
			<h2>Facets</h2>
			<DatasetProvenance dataset={name} {versions} />
			<GovernancePanel dataset={name} />
			<GrantsPanel dataset={name} client={grantsClient} />
			<ReadersPanel dataset={name} />

			<div class="rel">
				<div class="rel-group">
					<span class="rel-label">Upstream</span>
					{#each upstream as u (u.name)}
						<a class="rel-chip mono" href={`${base}/lineage/datasets/${encodeURIComponent(u.name)}`}
							>{u.name}</a
						>
					{:else}
						<span class="mut">none — a source dataset</span>
					{/each}
				</div>
				<div class="rel-group">
					<span class="rel-label">Downstream</span>
					{#each downstream as d (d.name)}
						<a class="rel-chip mono" href={`${base}/lineage/datasets/${encodeURIComponent(d.name)}`}
							>{d.name}</a
						>
					{:else}
						<span class="mut">none — nothing derives from it yet</span>
					{/each}
				</div>
			</div>
		</section>

		<section class="runs" {@attach enter({ y: 6, delay: 0.05 })}>
			<h2>Latest runs <span class="count mono">{producers.length}</span></h2>
			{#if producers.length === 0}
				<p class="hint">No producing runs recorded for this dataset yet.</p>
			{/if}
			{#each producers as r (r.run_id)}
				<div class="run" class:fail={/FAIL|ABORT/i.test(r.event_type ?? '')}>
					<div class="run-top">
						<span class="badge" style:background={stateColor(r.event_type)}>
							{r.dataset_version ? `v${r.dataset_version}` : r.event_type}
						</span>
						{#if r.operation}<span class="op mono">{r.operation}</span>{/if}
						<span class="who">{r.author ?? '—'}</span>
					</div>
					<div class="who">{r.event_time}</div>
					{#if r.row_count != null || r.size_bytes != null}
						<div class="metrics mono">
							{#if r.row_count != null}{r.row_count.toLocaleString()} rows{/if}
							{#if r.size_bytes != null}<span class="dot">·</span>{fmtBytes(r.size_bytes)}{/if}
						</div>
					{/if}
					{#if r.quality_passed != null}
						<div class="metrics">
							<span class="qchip" class:pass={r.quality_passed} class:block={!r.quality_passed}>
								quality {r.quality_passed ? 'passed' : 'blocked'}{r.quality_assertions?.length
									? ` · ${r.quality_assertions.length} check${r.quality_assertions.length === 1 ? '' : 's'}`
									: ''}
							</span>
						</div>
					{/if}
					{#if r.error_message}<div class="err">{r.error_message}</div>{/if}
					<RunInputs runId={r.run_id} />
				</div>
			{/each}
		</section>
	</div>
</div>

<style>
	.page {
		max-width: 980px;
		margin: 0 auto;
		padding: 40px 20px;
		width: 100%;
	}
	.back {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		color: var(--mut);
		font-size: 12px;
		text-decoration: none;
		margin-bottom: 8px;
	}
	.back:hover {
		color: var(--ink);
	}
	header {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
		margin-bottom: 16px;
	}
	h1 {
		font-size: 18px;
		margin: 0;
		word-break: break-all;
	}
	.viewlink {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 3px 10px;
		border: 1px solid var(--line);
		border-radius: 999px;
		color: var(--mut);
		font-size: 11px;
		font-weight: 600;
		text-decoration: none;
	}
	.viewlink:hover {
		color: var(--ink);
		border-color: var(--accent);
	}
	.grid {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		gap: 24px;
	}
	@media (max-width: 760px) {
		.grid {
			grid-template-columns: minmax(0, 1fr);
		}
	}
	h2 {
		font-size: 13px;
		margin: 0 0 10px;
		text-transform: uppercase;
		letter-spacing: 0.4px;
		color: var(--mut);
	}
	.count {
		color: var(--faint);
		font-weight: 400;
	}
	.hint {
		color: var(--mut);
		font-size: 12px;
	}
	.mut {
		color: var(--faint);
		font-size: 11px;
	}
	.rel {
		margin-top: 12px;
		padding-top: 10px;
		border-top: 1px solid var(--line);
	}
	.rel-group {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 5px;
		margin-bottom: 6px;
	}
	.rel-label {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.4px;
		color: var(--mut);
		margin-right: 2px;
	}
	.rel-chip {
		font-size: 11px;
		padding: 2px 8px;
		border-radius: 999px;
		border: 1px solid var(--line);
		background: var(--panel);
		color: var(--ink);
		text-decoration: none;
		transition:
			border-color 0.2s var(--ease),
			background 0.2s var(--ease);
	}
	.rel-chip:hover {
		border-color: var(--accent);
		background: color-mix(in srgb, var(--accent) 14%, transparent);
	}
	.badge {
		display: inline-block;
		padding: 1px 8px;
		border-radius: 999px;
		font-size: 11px;
		font-weight: 700;
		color: #06210f;
	}
	.run {
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 8px 10px;
		margin-bottom: 8px;
		font-size: 12px;
		background: linear-gradient(180deg, var(--panel-2), var(--panel));
		transition: border-color 0.2s var(--ease);
	}
	.run:hover {
		border-color: var(--line-2);
	}
	.run.fail {
		border-color: color-mix(in srgb, var(--fail) 55%, var(--line));
	}
	.run-top {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.run-top .who {
		margin-top: 0;
		margin-left: auto;
	}
	.who {
		color: var(--mut);
		font-size: 11px;
		margin-top: 4px;
	}
	.err {
		color: var(--fail);
		margin-top: 4px;
	}
	.op {
		font-size: 10.5px;
		color: var(--mut);
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm, 4px);
		padding: 0 5px;
	}
	.metrics {
		color: var(--mut);
		font-size: 11px;
		margin-top: 4px;
	}
	.metrics .dot {
		margin: 0 5px;
		color: var(--faint);
	}
	.qchip {
		font-size: 10.5px;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm, 4px);
		padding: 0 6px;
	}
	.qchip.pass {
		border-color: color-mix(in srgb, var(--ok) 55%, var(--line));
		color: var(--ok);
	}
	.qchip.block {
		border-color: color-mix(in srgb, var(--fail) 55%, var(--line));
		color: var(--fail);
	}
</style>
