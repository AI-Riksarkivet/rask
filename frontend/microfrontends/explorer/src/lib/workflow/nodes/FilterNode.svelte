<script lang="ts">
	/** Metadata filter. Emits `{ where, filters }` — merged into the Search
	 *  request so the search runs over a narrowed slice of the dataset. The facet
	 *  inputs are built from the descriptor's declared filterable fields, so the
	 *  node names no corpus column. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { activeView } from '@rask/explorer-api';
	import { graph } from '$lib/workflow/graph.svelte';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);

	// Declared filterable fields, each with a human label (from a matching
	// metadata declaration, else the raw field name).
	const view = $derived(activeView());
	const fields = $derived(
		view.filterFields.map((field) => ({
			field,
			label: view.metadataFields.find((m) => m.field === field)?.label ?? field,
		})),
	);
</script>

{#if cfg && rt}
	<NodeShell {id} title="Filter" status={rt.status} {selected}>
		<div class="flex flex-col gap-2">
			{#each fields as f (f.field)}
				<div>
					<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="{f.field}-{id}"
						>{f.label}</label
					>
					<input
						id="{f.field}-{id}"
						class="{FIELD_CLASS} w-full"
						placeholder="{f.label.toLowerCase()} contains…"
						value={cfg.filters[f.field] ?? ''}
						oninput={(e) =>
							graph.setConfig(id, {
								filters: { ...cfg.filters, [f.field]: e.currentTarget.value },
							})}
					/>
				</div>
			{/each}
			<div>
				<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="where-{id}"
					>SQL where</label
				>
				<input
					id="where-{id}"
					class="{FIELD_CLASS} w-full font-mono"
					placeholder="duration > 30"
					bind:value={cfg.where}
				/>
			</div>
		</div>
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
