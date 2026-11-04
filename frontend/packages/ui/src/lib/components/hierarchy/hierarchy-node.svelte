<script lang="ts" module>
	import type { Node, NodeProps } from '@xyflow/svelte';

	export type HierarchyTier = 'project' | 'warehouse' | 'namespace' | 'table';
	export type HierarchyData = {
		label: string;
		tier: HierarchyTier;
		/** Sub-line under the name (a warehouse's bucket, a "+k more" count). */
		detail?: string;
		/** The capped-remainder node — dashed, muted, honest. */
		more?: boolean;
		/** A rung whose read failed — destructive chrome, never silently absent. */
		err?: boolean;
		/** Where this rung navigates. Built by the CALLER (`ProjectHierarchy`'s `hrefFor`), because a
		 *  page's routes are the console's own and this library is mounted by two of them. Absent leaves
		 *  the rung a plain box — which is what `more` and `err` nodes are, since neither names an
		 *  object you can open. */
		href?: string;
	};
	export type HierarchyNodeType = Node<HierarchyData, 'rung'>;
</script>

<script lang="ts">
	// One rung of the project hierarchy (#104) — the SAME visual language as the FGA access
	// explorer's nodes (AccessGraphNode): a lucide icon per object type, a tier caption so the
	// graph says WHAT each box is, token-utility chrome only (themes in both modes).
	import { Handle, Position } from '@xyflow/svelte';
	import { Boxes, Database, Folder, Warehouse } from '@lucide/svelte';
	import { cn } from '../../utils/cn.js';

	let { data }: NodeProps<HierarchyNodeType> = $props();

	// The graph was a PICTURE: this rendered a plain `<div>` with no href and no handler, so the one
	// view that draws project › warehouse › namespace › table could not take you to any of them. It sat
	// beside a separate text drill-down, two representations of one hierarchy with nothing linking them
	// (TODO 4a asked for a map).

	const Tag = $derived(data.href ? 'a' : 'div');

	const ICONS = { project: Boxes, warehouse: Warehouse, namespace: Folder, table: Database };
	const Icon = $derived(ICONS[data.tier]);

	const TIER_CLASS: Record<HierarchyTier, string> = {
		project: 'border-primary bg-primary/10 ring-2 ring-primary/20',
		warehouse: 'border-success/60 bg-success/10',
		namespace: 'border-border bg-card',
		table: 'border-border/70 bg-muted/40',
	};
</script>

<svelte:element
	this={Tag}
	href={data.href}
	data-slot="hierarchy-node"
	role={data.href ? 'link' : undefined}
	class={cn(
		'flex min-w-32 items-center gap-2 rounded-lg border px-3 py-2 text-left shadow-sm',
		TIER_CLASS[data.tier],
		data.more && 'border-dashed opacity-70',
		data.err && 'border-destructive text-destructive',
		// Only a linked rung advertises itself as one. Keyboard reach comes free with the anchor.
		data.href &&
			'no-underline transition-colors hover:border-primary hover:bg-primary/5 focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none',
	)}
>
	<Icon class="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
	<div class="min-w-0 leading-tight">
		<div class="text-muted-foreground text-[10px] tracking-wide uppercase">{data.tier}</div>
		<div class="truncate text-sm font-medium">{data.label}</div>
		{#if data.detail}<div class="text-muted-foreground truncate text-xs">{data.detail}</div>{/if}
	</div>
	<Handle type="target" position={Position.Top} class="!opacity-0" />
	<Handle type="source" position={Position.Bottom} class="!opacity-0" />
</svelte:element>
