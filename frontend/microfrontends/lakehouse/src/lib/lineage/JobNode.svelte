<script lang="ts" module>
	import type { Node, NodeProps } from '@xyflow/svelte';

	export type JobData = {
		id: string;
		author?: string | null;
		state?: string | null;
		outputs: string[];
		failed: boolean;
		selected: boolean;
		/** Which side of the focused node this sits on — `null` when nothing is focused. Marquez
		 *  answers the same question with findUpstreamNodes/findDownstreamNodes. */
		rel?: 'focus' | 'upstream' | 'downstream' | null;
	};
	export type JobNodeType = Node<JobData, 'job'>;
</script>

<script lang="ts">
	import { Handle, Position } from '@xyflow/svelte';
	import { Cpu } from '@lucide/svelte';
	import { pop } from '@rask/ui/motion';

	let { data }: NodeProps<JobNodeType> = $props();

	const failed = $derived(data.failed || /FAIL|ABORT/i.test(data.state ?? ''));
	const done = $derived(data.state === 'COMPLETE');
	const ring = $derived(failed ? 'var(--fail)' : done ? 'var(--ok)' : 'var(--amber)');
	const stateKey = $derived(data.state ?? '');
</script>

<div
	class="job-node"
	class:selected={data.selected}
	data-rel={data.rel ?? undefined}
	style:--ring={ring}
	{@attach pop(stateKey)}
>
	<Handle type="target" position={Position.Left} />
	<div class="bar"></div>
	<div class="body">
		<div class="name"><Cpu size={13} /> {data.id}</div>
		<div class="meta">
			{data.author ?? '—'}{#if data.state}
				· {data.state}{/if}
		</div>
		{#if data.outputs.length}
			<div class="out mono">→ {data.outputs.join(', ')}</div>
		{/if}
	</div>
	<Handle type="source" position={Position.Right} />
</div>

<style>
	.job-node {
		display: flex;
		width: 210px;
		border: 1.5px solid var(--ring);
		border-radius: var(--radius);
		background: linear-gradient(180deg, var(--panel-2), var(--panel));
		overflow: hidden;
		box-shadow: var(--shadow);
		font-family: ui-sans-serif, system-ui, sans-serif;
	}
	.job-node.selected {
		outline: 2px solid #46f9b8;
		outline-offset: 2px;
	}
	.bar {
		width: 6px;
		background: var(--ring);
	}
	.body {
		padding: 8px 10px;
		min-width: 0;
	}
	.name {
		display: flex;
		align-items: center;
		gap: 5px;
		font-weight: 600;
		font-size: 13px;
		color: var(--ink);
	}
	.meta {
		font-size: 10.5px;
		color: var(--mut);
		margin-top: 3px;
	}
	.out {
		font-size: 10px;
		color: var(--accent);
		margin-top: 3px;
		word-break: break-all;
	}

	/* FOCUS CONTEXT. With a node focused, everything else on the canvas is context rather than
	   subject: upstream ("where this came from") and downstream ("what depends on it") are the two
	   questions a lineage graph exists to answer, and an undifferentiated blob answers neither.
	   Marquez separates the same two sets explicitly (findUpstreamNodes / findDownstreamNodes).
	   A tinted left border rather than a full re-colour: the medallion tier is already carried by
	   the card's own accent, and overriding it would trade one fact for another. */
	.job-node[data-rel='upstream'] {
		border-left: 3px solid var(--primary);
	}
	.job-node[data-rel='downstream'] {
		border-left: 3px solid var(--amber);
	}
	.job-node[data-rel='focus'] {
		border-left: 3px solid var(--ink);
	}
</style>
