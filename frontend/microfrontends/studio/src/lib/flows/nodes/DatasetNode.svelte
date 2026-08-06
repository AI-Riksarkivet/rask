<script lang="ts">
	/** Lakehouse table — the governed-data source, and the one node here that is openly a SCAFFOLD.
	 *
	 *  It exists so the lakehouse→flow seam is visible in the palette and in a drawn graph: the
	 *  interesting flows read rows out of a governed Lance table, not only an uploaded file. It does
	 *  NOT read rows: that is the explorer's Arrow lane (`/api/explorer/**`, keep-bytes, so a
	 *  `+server.ts` of its own), and faking rows here would let a flow read as working when it is
	 *  not. Running it raises instead — see `executor.ts`'s `dataset` case. The badge says so on the
	 *  card, the same way the train zone badges its placeholder pages. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
</script>

{#if cfg && rt}
	<NodeShell {id} title="Lakehouse table" status={rt.status} {selected}>
		<span
			class="mb-1 inline-block rounded-md bg-amber-500/15 px-1 text-[9px] tracking-wide text-amber-600 uppercase dark:text-amber-400"
			title="This node does not read rows yet — running it says so rather than emitting fake data"
		>
			scaffold
		</span>
		<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="table-{id}">Table</label>
		<input
			id="table-{id}"
			class="{FIELD_CLASS} w-full"
			placeholder="warehouse.namespace.table"
			bind:value={cfg.datasetTable}
		/>
		<p class="text-muted-foreground mt-1 text-[0.65rem]">
			Reading rows needs the Arrow lane — the shape is here, the transport is not.
		</p>
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
