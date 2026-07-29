<script lang="ts">
	/** Bezier edge that derives its OWN look from the graph — stroke colour by
	 *  payload, a label, and a dashed "running" pulse while the node it feeds runs.
	 *  Nothing mutates edge state from outside. The reconnect anchors make
	 *  endpoints draggable (validity gated by the flow's isValidConnection; the
	 *  anchor applies the rewire to the store). Selecting the edge reveals a ✕
	 *  button that disconnects it without touching the nodes. */
	import { getBezierPath } from '@xyflow/system';
	import { BaseEdge, EdgeLabel, EdgeReconnectAnchor, type EdgeProps } from '@xyflow/svelte';
	import { X } from '@lucide/svelte';
	import { graph } from '$lib/workflow/graph.svelte';
	import { edgePayload } from '$lib/workflow/edges';

	let {
		id,
		source,
		target,
		sourceX,
		sourceY,
		targetX,
		targetY,
		sourcePosition,
		targetPosition,
		markerEnd,
		label,
		selected,
	}: EdgeProps = $props();

	let [path, labelX, labelY] = $derived(
		getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition }),
	);

	const payload = $derived(edgePayload(graph.kindOf(source), graph.kindOf(target)));
	// Pulse the edge while the node it feeds is running.
	const running = $derived(graph.runtime[target]?.status === 'running');
	const edgeLabel = $derived(label ?? payload.label);
	const style = $derived(
		`stroke: ${payload.color}; stroke-width: 1.5;` +
			(running ? ' stroke-dasharray: 6; animation: edge-dash 0.7s linear infinite;' : ''),
	);
</script>

<BaseEdge
	{path}
	{labelX}
	{labelY}
	label={edgeLabel}
	{style}
	{...markerEnd !== undefined ? { markerEnd } : {}}
/>
<EdgeReconnectAnchor type="source" position={{ x: sourceX, y: sourceY }} />
<EdgeReconnectAnchor type="target" position={{ x: targetX, y: targetY }} />
{#if selected}
	<EdgeLabel x={labelX} y={labelY - 18}>
		<button
			type="button"
			class="border-border bg-card text-muted-foreground hover:bg-destructive/15 hover:text-destructive grid size-5 place-items-center rounded-full border shadow transition-colors"
			title="Disconnect edge"
			aria-label="Disconnect edge"
			onclick={(e) => {
	e.stopPropagation();
	graph.removeEdge(id);
}}
		>
			<X class="size-3" />
		</button>
	</EdgeLabel>
{/if}

<style>
	/* Global keyframe so the inline `animation: edge-dash` on the path resolves
     (Svelte scopes keyframe names by default; `-global-` opts that one out). */
	@keyframes -global-edge-dash {
		to {
			stroke-dashoffset: -12;
		}
	}
</style>
