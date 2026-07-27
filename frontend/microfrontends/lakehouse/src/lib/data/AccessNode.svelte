<script lang="ts" module>
	import type { Node, NodeProps } from '@xyflow/svelte';

	export type AccessData = {
		fgaId: string; // the full FGA id, e.g. user:alice / table:db1$t / role:eng#assignee
		fgaType: string; // user | role | team | table | namespace | warehouse | project
		label: string; // the id without its type prefix
		focus: boolean; // the object the graph is centred on
		selected: boolean;
	};
	export type AccessNodeType = Node<AccessData, 'access'>;
</script>

<script lang="ts">
	import { Handle, Position } from '@xyflow/svelte';
	import {
		Boxes,
		Database,
		Folder,
		ShieldCheck,
		User,
		Users,
		Warehouse as WarehouseIcon,
	} from '@lucide/svelte';
	import { pop } from '@rask/ui/motion';

	let { data }: NodeProps<AccessNodeType> = $props();

	// icon + accent per FGA type. Subjects (who) are blue; the focus object amber; containers grey.
	const ICONS: Record<string, typeof User> = {
		user: User,
		role: ShieldCheck,
		team: Users,
		table: Database,
		namespace: Folder,
		warehouse: WarehouseIcon,
		project: Boxes,
	};
	const Icon = $derived(ICONS[data.fgaType] ?? Database);
	const kind = $derived(
		['user', 'role', 'team'].includes(data.fgaType)
			? 'subject'
			: data.focus
				? 'focus'
				: 'container',
	);
	const accent = $derived(
		kind === 'subject' ? '#6aa9ff' : kind === 'focus' ? 'var(--amber, #ffc14d)' : '#8aa0bd',
	);
</script>

<div
	class="node {kind}"
	class:selected={data.selected}
	style:--accent={accent}
	{@attach pop(data.fgaId)}
>
	<Handle type="target" position={Position.Left} />
	<Icon size={13} />
	<div class="body">
		<div class="label mono">{data.label}</div>
		<div class="type">{data.fgaType}</div>
	</div>
	<Handle type="source" position={Position.Right} />
</div>

<style>
	.node {
		display: flex;
		align-items: center;
		gap: 7px;
		min-width: 120px;
		max-width: 200px;
		padding: 6px 10px;
		border: 1.5px solid var(--accent);
		border-radius: var(--radius-sm, 6px);
		background: linear-gradient(180deg, var(--panel-2), var(--panel));
		color: var(--ink);
		font-family: ui-sans-serif, system-ui, sans-serif;
		box-shadow: var(--shadow, 0 2px 8px rgb(0 0 0 / 0.2));
	}
	.node.focus {
		border-width: 2px;
	}
	.node.selected {
		outline: 2px solid #46f9b8;
		outline-offset: 2px;
	}
	.body {
		min-width: 0;
	}
	.label {
		font-size: 12px;
		font-weight: 600;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.type {
		font-size: 9.5px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--faint);
	}
</style>
