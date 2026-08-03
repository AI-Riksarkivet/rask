<script lang="ts">
	/**
	 * The read-only Svelte Flow viewer, once.
	 *
	 * Five components in this estate mounted `<SvelteFlow>` with the same chrome — dotted background,
	 * Controls, `fitView`, an auto-refit on data change — and each wired it slightly differently. The
	 * cost was not the duplicated lines: it was that a fix landed in one copy. `FlowAutoFit` existed
	 * twice with DIFFERENT bodies, and only the newer one passed `maxZoom: 1`, so the same three-node
	 * graph rendered at natural size in the lineage panel and blown up to fill the canvas next door.
	 *
	 * Deliberately NOT for the workflow editor (`media/lib/workflow/FlowPane`). That one is a real
	 * editor — `onconnect`, `onbeforeconnect`, drag-to-connect validation — and folding an editor into a
	 * viewer shell produces a component with two modes and one confused prop list. Viewers share; the
	 * editor stays its own thing.
	 *
	 * The svelte-flow rules this encodes so callers cannot get them wrong:
	 *   • `bind:nodes` / `bind:edges` — a plain prop never writes drag/select back to the caller.
	 *   • `nodeTypes` stays the CALLER's, declared at ITS module scope; passing it through here keeps
	 *     that requirement where the component being registered actually lives.
	 *   • The wrapper is sized by its parent; a zero-height container renders an empty canvas, so the
	 *     caller owns layout and this owns only the flow.
	 *
	 * The stylesheet is imported once per zone in `app.css` (vendor sheet + the rask `--xy-*` theme,
	 * both under `layer(base)`), never here — a component-level import lands it in that component's
	 * layer and loses to it.
	 */
	import { Background, BackgroundVariant, Controls, MiniMap, SvelteFlow } from '@xyflow/svelte';
	import type { Edge, EdgeTypes, FitViewOptions, Node, NodeTypes } from '@xyflow/svelte';
	import type { Snippet } from 'svelte';
	import FlowAutoFit from './FlowAutoFit.svelte';

	type Props = {
		nodes: Node[];
		edges: Edge[];
		nodeTypes?: NodeTypes;
		edgeTypes?: EdgeTypes;
		/**
		 * A signature of the current node set. When it changes the viewport re-frames; when it does not,
		 * a poll that returns the same graph leaves the user's pan/zoom alone. Pass `undefined` to opt
		 * out of auto-fitting entirely (a canvas the user is expected to arrange by hand).
		 */
		fitTrigger?: string | undefined;
		/** Screen-space gutter for the refit — widen it when floating overlays cover the canvas edges. */
		fitPadding?: FitViewOptions['padding'];
		minimap?: boolean;
		/** Dot spacing. The default matches the lineage and access canvases. */
		backgroundGap?: number;
		/** Absolutely-positioned chrome — badges, a legend, a Panel. Rendered over the canvas, inside
		 *  the same positioned wrapper, so a caller does not have to build its own stacking context. */
		overlay?: Snippet;
	};

	let {
		nodes = $bindable(),
		edges = $bindable(),
		nodeTypes,
		edgeTypes,
		fitTrigger,
		fitPadding = 0.22,
		minimap = false,
		backgroundGap = 16,
		overlay,
		...rest
	}: Props & Record<string, unknown> = $props();
</script>

<div class="relative h-full w-full" data-slot="graph-canvas">
	<SvelteFlow bind:nodes bind:edges {nodeTypes} {edgeTypes} fitView {...rest}>
		<Background variant={BackgroundVariant.Dots} gap={backgroundGap} />
		<Controls />
		{#if minimap}<MiniMap />{/if}
		{#if fitTrigger !== undefined}
			<FlowAutoFit trigger={fitTrigger} padding={fitPadding} />
		{/if}
	</SvelteFlow>
	{@render overlay?.()}
</div>
