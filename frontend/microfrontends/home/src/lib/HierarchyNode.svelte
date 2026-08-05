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
	};
	export type HierarchyNodeType = Node<HierarchyData, 'rung'>;
</script>

<script lang="ts">
	// One rung of the project hierarchy (#104) — the SAME visual language as the FGA access
	// explorer's nodes (AccessGraphNode): a lucide icon per object type, a tier caption so the
	// graph says WHAT each box is, token-utility chrome only (themes in both modes).
	import { Handle, Position } from '@xyflow/svelte';
	import { Boxes, Database, Folder, Warehouse } from '@lucide/svelte';
	import { cn } from '@rask/ui/utils';

	let { data }: NodeProps<HierarchyNodeType> = $props();

	const ICONS = { project: Boxes, warehouse: Warehouse, namespace: Folder, table: Database };
	const Icon = $derived(ICONS[data.tier]);

	const TIER_CLASS: Record<HierarchyTier, string> = {
		project: 'border-primary bg-primary/10 ring-2 ring-primary/20',
		warehouse: 'border-success/60 bg-success/10',
		namespace: 'border-border bg-card',
		table: 'border-border/70 bg-muted/40',
	};
</script>

<div
	class={cn(
	'flex min-w-32 items-center gap-2 rounded-lg border px-3 py-2 text-left shadow-sm',
	TIER_CLASS[data.tier],
	data.more && 'border-dashed opacity-70',
	data.err && 'border-destructive text-destructive',
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
</div>
