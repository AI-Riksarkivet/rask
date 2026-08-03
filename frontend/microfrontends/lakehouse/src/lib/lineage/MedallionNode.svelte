<script lang="ts" module>
	import type { Node, NodeProps } from '@xyflow/svelte';

	export type MedallionData = {
		id: string;
		layer: number;
		source_uri?: string | null;
		tags: string[];
		versions: string[];
		failed: boolean;
		selected: boolean;
		runState?: string | null;
	};
	export type MedallionNodeType = Node<MedallionData, 'medallion'>;

	const COLORS = ['#ff9457', '#cd7f32', '#9fb6cf', '#ffc14d', '#8aa0bd'];

	/** A busy table writes dozens of versions; one chip each turned the card into a green wall
	 * that buried the name, the URI and the failure badge. Show the first few and roll the rest
	 * into a `+N` — the full list stays available in the hover title. */
	const MAX_VERSION_CHIPS = 3;
</script>

<script lang="ts">
	import { Handle, Position } from '@xyflow/svelte';
	import { Boxes, Database, Layers, Gem } from '@lucide/svelte';
	import { pulse, pop } from '@rask/ui/motion';

	let { data }: NodeProps<MedallionNodeType> = $props();

	// Icon per medallion layer (raw → bronze → silver → gold).
	const LAYER_ICONS = [Boxes, Database, Layers, Gem, Database];
	const LayerIcon = $derived(LAYER_ICONS[data.layer] ?? Database);
	const color = $derived(COLORS[data.layer] ?? COLORS[4]);
	// Derived *primitives* so the continuous pulse only re-inits when the value actually flips,
	// not on every 2s poll (which reassigns `data`).
	const running = $derived(/START|RUNNING/i.test(data.runState ?? ''));
	const done = $derived(data.runState === 'COMPLETE');
	const failedRun = $derived(/FAIL|ABORT/i.test(data.runState ?? ''));
	const stateKey = $derived(data.runState ?? '');
	const ring = $derived(
		failedRun ? 'var(--fail)' : done ? 'var(--ok)' : running ? 'var(--amber)' : color,
	);
	const shownVersions = $derived(data.versions.slice(0, MAX_VERSION_CHIPS));
	const hiddenVersions = $derived(Math.max(0, data.versions.length - MAX_VERSION_CHIPS));
	const versionsTitle = $derived(
		data.versions.length
			? `${data.versions.length} version${data.versions.length === 1 ? '' : 's'} written: ${data.versions
					.map((v) => `v${v}`)
					.join(', ')}`
			: 'no versions written yet',
	);
</script>

<div
	class="node"
	class:selected={data.selected}
	style:--accent={color}
	style:--ring={ring}
	{@attach pop(stateKey)}
	{@attach pulse(running, '255, 193, 77')}
>
	<Handle type="target" position={Position.Left} />
	<div class="bar"></div>
	<div class="body">
		<div class="name" title={data.id}><LayerIcon size={12} {color} /> <span>{data.id}</span></div>
		<div class="uri" title={data.source_uri ?? undefined}>{data.source_uri ?? '(pending)'}</div>
		<div class="chips">
			{#if data.versions.length}
				<span class="versions" title={versionsTitle}>
					{#each shownVersions as v (v)}
						<span class="chip ok">v{v}</span>
					{/each}
					{#if hiddenVersions}
						<span class="chip more">+{hiddenVersions}</span>
					{/if}
				</span>
			{/if}
			{#if data.failed}
				<span class="chip fail">⚠ failed</span>
			{/if}
			{#each data.tags as t (t)}
				<span class="chip tag">{t}</span>
			{/each}
		</div>
	</div>
	<Handle type="source" position={Position.Right} />
</div>

<style>
	/* Compact card (~200×64): the graph frames dozens of these, so density beats roominess. */
	.node {
		display: flex;
		width: 200px;
		border: 1.5px solid var(--ring, var(--accent));
		border-radius: var(--radius);
		background: linear-gradient(180deg, var(--panel-2), var(--panel));
		overflow: hidden;
		font-family: ui-sans-serif, system-ui, sans-serif;
		box-shadow: var(--shadow);
		transition: border-color 0.35s var(--ease);
	}
	.node.selected {
		outline: 2px solid #46f9b8;
		outline-offset: 2px;
	}
	.bar {
		width: 4px;
		background: var(--accent);
	}
	.body {
		padding: 5px 8px 6px;
		min-width: 0;
		flex: 1;
	}
	.name {
		display: flex;
		align-items: center;
		gap: 4px;
		font-weight: 600;
		font-size: 12px;
		color: var(--ink);
	}
	/* Long ids/URIs truncate (full value in the title tooltip) instead of growing the card. */
	.name > span,
	.uri {
		overflow: hidden;
		white-space: nowrap;
		text-overflow: ellipsis;
	}
	.uri {
		font-size: 10px;
		color: var(--mut);
		margin: 1px 0 4px;
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 3px;
	}
	/* One hover target for the whole version run — the title carries the full list. */
	.versions {
		display: inline-flex;
		align-items: center;
		gap: 3px;
	}
	.chip {
		font-size: 9.5px;
		font-weight: 700;
		padding: 0.5px 6px;
		border-radius: 999px;
	}
	.chip.ok {
		background: var(--ok);
		color: #06210f;
	}
	.chip.fail {
		background: var(--fail);
		color: #2a0307;
	}
	.chip.more {
		background: color-mix(in srgb, var(--ok) 22%, transparent);
		color: var(--ok);
		font-variant-numeric: tabular-nums;
	}
	.chip.tag {
		background: var(--panel-2);
		color: var(--mut);
		font-weight: 600;
	}
</style>
