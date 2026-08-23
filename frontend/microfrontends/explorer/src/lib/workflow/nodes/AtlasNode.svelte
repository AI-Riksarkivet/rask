<script lang="ts">
	/** Atlas selection source. No longer a passive reader of the live global
	 *  crossFilter: the node shows a compact summary of its CAPTURED selection +
	 *  an "Open atlas" button that launches the full interactive AtlasMap in a
	 *  modal, PRE-FILTERED to this node's upstream results. Confirming in the modal
	 *  captures the hit set onto the node's config; the executor emits exactly that
	 *  downstream (so /atlas and the workflow no longer share live map state). */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { Map as MapIcon } from '@lucide/svelte';
	import { graph } from '$lib/workflow/graph.svelte';
	import type { Hit } from '@rask/explorer-api';
	import NodeShell from './NodeShell.svelte';
	import AtlasModal from './AtlasModal.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
	const capturedCount = $derived(cfg?.capturedAtlasSelection?.length ?? 0);

	let modalOpen = $state(false);
	/** Snapshot of the upstream hits taken when the modal opens (drives pre-filter). */
	let upstreamHits = $state<Hit[] | null>(null);

	function openModal(): void {
		upstreamHits = graph.getPredecessorHits(id);
		modalOpen = true;
	}

	function onConfirm(hits: Hit[]): void {
		graph.setConfig(id, { capturedAtlasSelection: hits.length ? hits : null });
		modalOpen = false;
	}

	function onCancel(): void {
		modalOpen = false;
	}
</script>

{#if cfg && rt}
	<NodeShell {id} title="Atlas selection" status={rt.status} {selected}>
		<div class="flex flex-col gap-2">
			<p class="text-foreground text-sm font-medium">
				{capturedCount.toLocaleString()} points selected
			</p>
			<button
				type="button"
				class="nodrag border-border bg-background text-foreground hover:bg-muted inline-flex items-center justify-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium transition-colors"
				onclick={(e) => {
					e.stopPropagation();
					openModal();
				}}
			>
				<MapIcon class="size-3" />
				Open atlas
			</button>
			{#if capturedCount === 0}
				<p class="text-muted-foreground text-[0.7rem]">
					Open the <span class="text-amber-500">Atlas</span> viewer, lasso a region, and
					<span class="text-foreground">Confirm</span> to capture it into the workflow.
				</p>
			{:else}
				<p class="text-[0.7rem] text-emerald-500">
					Selection captured. Wire into Search to refine within it.
				</p>
			{/if}
		</div>
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}

{#if modalOpen}
	<AtlasModal {upstreamHits} {onConfirm} {onCancel} />
{/if}
