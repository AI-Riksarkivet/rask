/**
 * Edge presentation vocabulary — the arrowhead marker + a stroke colour/label
 * derived from what an edge carries (its SOURCE node kind, and for results where
 * it goes). Pure functions; the ReconnectableEdge component derives its own look
 * from these, so there's no canvas effect mutating edge state. `NodeKind` is a
 * type-only import (erased at runtime), so there's no cycle with graph.svelte.ts.
 */
import { MarkerType } from '@xyflow/svelte';
import type { NodeKind } from './graph.svelte';

/** Neutral arrowhead colour (the per-edge STROKE carries the payload colour). */
const ARROW_COLOR = '#94a3b8';
const ARROW_SIZE = 18;

/** Default arrowhead for every edge — used in defaultEdgeOptions. */
export const ARROW_MARKER = {
	type: MarkerType.ArrowClosed,
	width: ARROW_SIZE,
	height: ARROW_SIZE,
	color: ARROW_COLOR,
};

/** Per-payload stroke colours (readable on dark + light). */
const PAYLOAD_COLOR: Record<string, string> = {
	query: '#f59e0b', // amber  — text query spec
	image: '#8b5cf6', // violet — image spec
	filter: '#64748b', // slate  — metadata filter spec
	atlas: '#10b981', // emerald — atlas selection result set
	results: '#10b981', // emerald — a concrete result set
	refine: '#10b981', // emerald — results used to scope a downstream Search
	tagged: '#22c55e', // green   — tagged results
	default: ARROW_COLOR,
};

/** What an edge carries (label + stroke colour), from its source (and, for
 *  result-producing sources, whether it feeds a Search → "refine"). */
export function edgePayload(
	source: NodeKind | null,
	target: NodeKind | null,
): { label: string; color: string } {
	let label: string;
	if (source === 'query' || source === 'image' || source === 'filter') label = source;
	else if (source === 'atlas') label = target === 'search' ? 'refine' : 'results';
	else if (source === 'tagger') label = 'tagged';
	else if (source === 'search' || source === 'combine')
		label = target === 'search' ? 'refine' : 'results';
	else label = 'results';
	return { label, color: PAYLOAD_COLOR[label] ?? PAYLOAD_COLOR.default! };
}
