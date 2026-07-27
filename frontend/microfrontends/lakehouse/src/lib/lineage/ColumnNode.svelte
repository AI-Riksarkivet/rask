<script lang="ts" module>
	import type { Node, NodeProps } from '@xyflow/svelte';

	export type ColumnData = {
		dataset: string;
		field: string;
		type?: string | null;
		layer: number;
		masked: boolean;
		isRoot: boolean;
	};
	export type ColumnNodeType = Node<ColumnData, 'column'>;

	// Same medallion palette as the dataset plane, so a column reads as belonging to its layer.
	const COLORS = ['#ff9457', '#cd7f32', '#9fb6cf', '#ffc14d', '#8aa0bd'];
	// A column's short dataset label (drop the namespace$ prefix that the table id carries).
	const shortDs = (ds: string) => ds.split('$').at(-1) ?? ds;
</script>

<script lang="ts">
	import { Handle, Position } from '@xyflow/svelte';
	import { Columns3, ShieldAlert } from '@lucide/svelte';

	let { data }: NodeProps<ColumnNodeType> = $props();
	const color = $derived(COLORS[data.layer] ?? COLORS[4]);
</script>

<div class="col" class:root={data.isRoot} class:masked={data.masked} style:--accent={color}>
	<Handle type="target" position={Position.Left} />
	<div class="bar"></div>
	<div class="body">
		<div class="field">
			<Columns3 size={11} {color} />
			<span class="name">{data.field}</span>
			{#if data.masked}<ShieldAlert size={11} class="mask-ic" />{/if}
		</div>
		<div class="meta">
			<span class="ds">{shortDs(data.dataset)}</span>
			{#if data.type}<span class="ty">{data.type}</span>{/if}
		</div>
	</div>
	<Handle type="source" position={Position.Right} />
</div>

<style>
	.col {
		display: flex;
		width: 170px;
		border: 1.5px solid color-mix(in srgb, var(--accent) 60%, var(--line));
		border-radius: 9px;
		background: linear-gradient(180deg, var(--panel-2), var(--panel));
		overflow: hidden;
		font-family: ui-sans-serif, system-ui, sans-serif;
		box-shadow: var(--shadow);
	}
	.col.root {
		border-color: var(--accent);
		outline: 1.5px solid color-mix(in srgb, var(--accent) 45%, transparent);
		outline-offset: 1px;
	}
	.col.masked {
		border-color: color-mix(in srgb, var(--fail) 60%, var(--line));
	}
	.bar {
		width: 5px;
		background: var(--accent);
	}
	.col.masked .bar {
		background: var(--fail);
	}
	.body {
		padding: 6px 9px;
		min-width: 0;
		flex: 1;
	}
	.field {
		display: flex;
		align-items: center;
		gap: 5px;
	}
	.name {
		font-weight: 600;
		font-size: 12px;
		color: var(--ink);
	}
	:global(.mask-ic) {
		color: var(--fail);
		margin-left: auto;
	}
	.meta {
		display: flex;
		justify-content: space-between;
		gap: 6px;
		margin-top: 3px;
	}
	.ds {
		font-size: 10px;
		color: var(--mut);
	}
	.ty {
		font-size: 10px;
		color: var(--faint);
		font-family: ui-monospace, monospace;
	}
</style>
