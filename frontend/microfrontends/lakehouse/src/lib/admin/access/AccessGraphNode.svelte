<script lang="ts" module>
	import type { Node, NodeProps } from '@xyflow/svelte';
	import type { NodeRole } from './graph';

	export type AccessData = {
		fgaId: string;
		fgaType: string;
		label: string;
		role: NodeRole;
		/** The mechanism that put this node here ("inherited from", "EXCLUDED by", …). Rendered on the
		 *  node itself, because a hop that does not say what it is turns the graph back into a picture. */
		via: string | null;
	};
	export type AccessNodeType = Node<AccessData, 'access'>;
</script>

<script lang="ts">
	// One node of the explorer canvas. Styled entirely with @rask/ui token utilities — no legacy bridge
	// vars, no literal hex — so it themes in both modes without a second source of truth.
	import { Handle, Position } from '@xyflow/svelte';
	import {
		Boxes,
		Database,
		Folder,
		Package,
		ShieldCheck,
		User,
		Users,
		Warehouse,
	} from '@lucide/svelte';
	import { cn } from '@rask/ui/utils';

	let { data }: NodeProps<AccessNodeType> = $props();

	const ICONS: Record<string, typeof User> = {
		user: User,
		role: ShieldCheck,
		team: Users,
		table: Database,
		namespace: Folder,
		warehouse: Warehouse,
		project: Boxes,
		model: Package,
	};
	const Icon = $derived(ICONS[data.fgaType] ?? Database);

	// Role → chrome. `dim` keeps context on screen at low contrast rather than removing it: an operator
	// has to be able to see that the rest of the graph still exists, or "filtered down" reads as "gone".
	const ROLE_CLASS: Record<NodeRole, string> = {
		focus: 'border-primary bg-primary/10 text-foreground ring-2 ring-primary/30',
		path: 'border-success bg-success/10 text-foreground',
		subject: 'border-border bg-card text-foreground',
		container: 'border-border bg-card text-foreground',
		dim: 'border-border/40 bg-card/40 text-muted-foreground opacity-50',
	};

	// "EXCLUDED by" is the one mechanism that means the OPPOSITE of the others — a subject the model
	// subtracts. It gets the destructive treatment so a `but not` can never be skimmed as a grant.
	const excluded = $derived(data.via?.startsWith('EXCLUDED') === true);
</script>

<div
	class={cn(
	'flex min-w-[140px] max-w-[220px] items-center gap-2 rounded-md border px-2.5 py-1.5 shadow-sm transition-opacity',
	ROLE_CLASS[data.role],
	excluded && 'border-destructive bg-destructive/10',
)}
	data-slot="access-node"
	data-role={data.role}
>
	<Handle type="target" position={Position.Left} />
	<Icon size={13} class="shrink-0" />
	<div class="min-w-0">
		<div class="truncate font-mono text-xs font-semibold">{data.label}</div>
		<div class="text-[9.5px] uppercase tracking-wider text-muted-foreground">
			{data.fgaType}{#if data.via}<span class={excluded ? 'text-destructive' : ''}>
					· {data.via}</span>{/if}
		</div>
	</div>
	<Handle type="source" position={Position.Right} />
</div>
