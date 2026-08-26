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
		/** Which side of the focused node this sits on — `null` when nothing is focused. Marquez
		 *  answers the same question with findUpstreamNodes/findDownstreamNodes. */
		rel?: 'focus' | 'upstream' | 'downstream' | null;
		runState?: string | null;
		/** Compact mode: name and status only. See the switch in `LineageGraph.svelte`. */
		compact?: boolean;
		/** True when this node's downstream is folded away. */
		collapsed?: boolean;
		/** Fold/unfold this node's downstream. Handed DOWN through data because the collapsed set is
		 *  the graph's (and the URL's), not the card's — a node that owned its own collapsed flag could
		 *  not survive the rebuild that collapsing causes. */
		onCollapse?: (id: string) => void;
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

	let { id, data }: NodeProps<MedallionNodeType> = $props();

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
	class:compact={data.compact}
	class:selected={data.selected}
	data-rel={data.rel ?? undefined}
	style:--accent={color}
	style:--ring={ring}
	{@attach pop(stateKey)}
	{@attach pulse(running, '255, 193, 77')}
>
	<Handle type="target" position={Position.Left} />
	<div class="bar"></div>
	<div class="body">
		<div class="name" title={data.id}>
			<LayerIcon size={12} {color} />
			<span>{data.id}</span>
			<!-- COMPACT KEEPS THE FAILURE, and that is the whole design of the compact card: what is
			     dropped is description (the URI, the version history, the tags) and what is kept is
			     anything a reader would act on. A density mode that hides a failed write makes a
			     broken table look healthy, which is worse than not having the mode. -->
			{#if data.compact && data.failed}<span class="dot fail" title="a producing run failed"></span>{/if}
			<!-- COLLAPSE. `nodrag` so grabbing the chevron does not start a drag, and `stopPropagation`
			     so it does not also re-root the graph — one gesture, one effect. -->
			{#if data.onCollapse}
				<button
					class="fold nodrag"
					aria-pressed={data.collapsed ?? false}
					title={data.collapsed ? 'expand what this feeds' : 'fold away what this feeds'}
					aria-label={data.collapsed ? 'Expand downstream' : 'Collapse downstream'}
					onclick={(e) => {
						e.stopPropagation();
						data.onCollapse?.(id);
					}}
				>
					{data.collapsed ? '\u203A' : '\u2039'}
				</button>
			{/if}
		</div>
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
	/* COMPACT: name and status only. At estate scale the canvas frames dozens of cards, and the
	   URI and chip rows are what push a card past 100px tall — the height ELK reserves, the height
	   that makes cards collide, and the height that forces `fitView` to zoom out until nothing is
	   readable. Narrower as well as shorter, because the wasted width is what strings a layered
	   graph across a canvas nobody can see at once. */
	.node.compact {
		width: 152px;
	}
	.node.compact .uri,
	.node.compact .chips {
		display: none;
	}
	.node.compact .body {
		padding: 4px 8px;
	}
	/* The chevron. Faint until the card is hovered — it is an affordance on every node, and at
	   estate scale eighty visible chevrons are noise. A COLLAPSED node keeps it lit, because that is
	   the only thing on the canvas saying something is folded behind this card. */
	.fold {
		flex: none;
		width: 15px;
		height: 15px;
		padding: 0;
		border: none;
		border-radius: 4px;
		background: transparent;
		color: var(--mut, var(--muted));
		font-size: 13px;
		line-height: 1;
		cursor: pointer;
		opacity: 0;
		transition: opacity 0.15s var(--ease);
	}
	.fold[aria-pressed='true'] {
		opacity: 1;
		color: var(--amber);
		background: color-mix(in srgb, var(--amber) 18%, transparent);
	}
	.node:hover .fold {
		opacity: 1;
	}
	.dot {
		flex: none;
		width: 7px;
		height: 7px;
		border-radius: 50%;
	}
	.dot.fail {
		background: var(--fail);
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

	/* FOCUS CONTEXT. With a node focused, everything else on the canvas is context rather than
	   subject: upstream ("where this came from") and downstream ("what depends on it") are the two
	   questions a lineage graph exists to answer, and an undifferentiated blob answers neither.
	   Marquez separates the same two sets explicitly (findUpstreamNodes / findDownstreamNodes).
	   A tinted left border rather than a full re-colour: the medallion tier is already carried by
	   the card's own accent, and overriding it would trade one fact for another. */
	.node[data-rel='upstream'] {
		border-left: 3px solid var(--primary);
	}
	.node[data-rel='downstream'] {
		border-left: 3px solid var(--amber);
	}
	.node[data-rel='focus'] {
		border-left: 3px solid var(--ink);
	}
</style>
