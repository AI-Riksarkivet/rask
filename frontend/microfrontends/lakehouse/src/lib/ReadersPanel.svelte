<script lang="ts">
	// The dataset panel's read-audit section (#41): WHO has read this dataset — the access-log twin of the
	// producers list (who WROTE it) and the sibling of GrantsPanel (who CAN reach it). Owner-gated on the
	// backend (can_write_data — an access log reveals who touched the data), so a non-owner sees the denial
	// state, never the log. Collapsed by default and lazy: the read-audit is off by default, so most opens
	// return an empty log — no reason to fire an owner-tier round-trip on every dataset click. Definitive
	// outcomes (the log, 401/403) cache per dataset; transient failures (offline/5xx) retry on the next open.
	import { ChevronRight, Eye } from '@lucide/svelte';
	import { fetchReaders } from './api';
	import type { ReaderInfo } from '@rask/api/lineage';

	let { dataset }: { dataset: string } = $props();

	// State keyed by the dataset it belongs to (no cross-dataset bleed — mirrors GrantsPanel): the panel is
	// open, loading, or showing a log only for the dataset it was produced for; switching blanks by derivation.
	let openedFor = $state<string | null>(null);
	let log = $state<{ for: string; readers: ReaderInfo[] | null; denied: string | null } | null>(
		null,
	);
	let loadingFor = $state<string | null>(null);
	let failedFor = $state<string | null>(null); // transient failure — never cached, reopen retries

	const open = $derived(openedFor === dataset);
	const shown = $derived(log?.for === dataset ? log : null);
	const loading = $derived(loadingFor === dataset);
	const failed = $derived(failedFor === dataset);

	async function toggle(): Promise<void> {
		if (open) {
			openedFor = null;
			return;
		}
		openedFor = dataset;
		if (shown !== null || loading) return;
		loadingFor = dataset;
		failedFor = null;
		const current = dataset;
		try {
			const res = await fetchReaders(current);
			// Latest-wins: the user clicked away while this was in flight — drop the stale result.
			if (dataset !== current) return;
			if (res.ok) {
				log = { for: current, readers: res.data.readers ?? [], denied: null };
			} else if (res.status === 401 || res.status === 403) {
				const denied =
					res.status === 401
						? 'Sign in to view the access log.'
						: 'Owner access required to see who read this dataset.';
				log = { for: current, readers: null, denied };
			} else {
				failedFor = current; // offline / 5xx: shown but not cached, so the next open retries
			}
		} finally {
			if (loadingFor === current) loadingFor = null;
		}
	}
</script>

<div class="readers">
	<button class="head" onclick={toggle} aria-expanded={open}>
		<span class="chev" class:open><ChevronRight size={12} /></span>
		<Eye size={12} />
		<span>Read by</span>
	</button>
	{#if open}
		{#if loading}
			<p class="mut">Loading the access log…</p>
		{:else if failed}
			<p class="mut">Access log unavailable right now — close and reopen to retry.</p>
		{:else if shown?.denied}
			<p class="mut">{shown.denied}</p>
		{:else if shown?.readers}
			{#if shown.readers.length === 0}
				<p class="mut">No reads recorded yet (the read-audit log is off by default).</p>
			{:else}
				<ul class="list">
					{#each shown.readers as r (r.reader)}
						<li class="row">
							<span class="who mono">{r.reader}</span>
							<span class="meta mono">
								{r.reads}
								{r.reads === 1 ? 'read' : 'reads'}{#if r.last_read}
									· last {r.last_read.slice(0, 19)}{/if}
							</span>
						</li>
					{/each}
				</ul>
			{/if}
		{/if}
	{/if}
</div>

<style>
	.readers {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin: 2px 0 10px;
	}
	.head {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		background: none;
		border: none;
		padding: 0;
		color: var(--mut);
		font: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.head:hover {
		color: var(--ink);
	}
	.chev {
		display: inline-flex;
		transition: transform 0.12s ease;
	}
	.chev.open {
		transform: rotate(90deg);
	}
	.list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 3px;
	}
	.row {
		display: flex;
		align-items: baseline;
		gap: 6px;
	}
	.who {
		font-size: 12px;
		color: var(--ink);
	}
	.meta {
		font-size: 10px;
		color: var(--faint);
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
		margin: 0;
	}
</style>
