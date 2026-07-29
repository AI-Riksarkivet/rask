<script lang="ts">
	// The reproducibility pins for one producing run (#115 D1): the inputs it consumed, each with the exact
	// version it READ — the answer to "which feature versions produced this output". The producing-runs list
	// shows each run's WROTE version; this adds the READ side. Lazy + per-run: the endpoint is deliberately
	// kept off the hot /runs board (an N+1 READ-edge fetch), so it loads only when the user expands one run.
	import { ArrowDownLeft, ChevronRight } from '@lucide/svelte';
	import { fetchRunInputs } from '$lib/api';
	import type { RunInput } from '@rask/api/lineage';

	let { runId }: { runId: string } = $props();

	let open = $state(false);
	let inputs = $state<RunInput[] | null>(null);
	let loaded = $state(false);

	async function toggle(): Promise<void> {
		open = !open;
		if (!open || loaded) return;
		loaded = true;
		const res = await fetchRunInputs(runId);
		inputs = res?.inputs ?? [];
	}
</script>

<div class="ri">
	<button class="toggle" onclick={toggle} aria-expanded={open}>
		<span class="chev" class:open><ChevronRight size={10} /></span>
		<ArrowDownLeft size={10} />
		reads
	</button>
	{#if open}
		{#if inputs === null}
			<span class="mut">loading…</span>
		{:else if inputs.length === 0}
			<span class="mut">no pinned inputs</span>
		{:else}
			<span class="pins">
				{#each inputs as i (i.name)}
					<span class="pin mono">
						{i.name}{#if i.version}<span class="ver">@{i.version}</span>{:else}<span class="latest">@latest</span>{/if}
					</span>
				{/each}
			</span>
		{/if}
	{/if}
</div>

<style>
	.ri {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 5px;
		margin-top: 3px;
	}
	.toggle {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		background: none;
		border: none;
		padding: 0;
		color: var(--faint);
		font: inherit;
		font-size: 10px;
		cursor: pointer;
	}
	.toggle:hover {
		color: var(--mut);
	}
	.chev {
		display: inline-flex;
		transition: transform 0.12s ease;
	}
	.chev.open {
		transform: rotate(90deg);
	}
	.pins {
		display: inline-flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.pin {
		font-size: 10px;
		color: var(--mut);
		border: 1px solid var(--line);
		border-radius: 999px;
		padding: 1px 7px;
	}
	.ver {
		color: var(--ink);
	}
	.latest {
		color: var(--faint);
		font-style: italic;
	}
	.mut {
		color: var(--faint);
		font-size: 10px;
	}
</style>
