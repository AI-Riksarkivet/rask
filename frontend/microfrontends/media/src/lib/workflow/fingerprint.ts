/**
 * Output fingerprint for a workflow node — a pure derivation over the node's
 * output-affecting config and incoming edges, kept out of the store class so
 * the invalidation rule is testable and readable in one place.
 *
 * Stored beside a node's cached output at compute time and compared at reuse
 * time: a mismatch means the node was edited or rewired since the output was
 * recorded, so the cache must not be served (this catches in-place `bind:`
 * config mutations that never pass through the store's setConfig).
 */
import type { Edge } from '@xyflow/svelte';
import { hitKey } from '$lib/utils';
import type { NodeConfig } from './types';

/** Structured filters as a stable, order-independent list of non-empty entries
 *  (a Record's key order would otherwise leak into the fingerprint). */
function stableFilters(filters: Record<string, string>): [string, string][] {
	return Object.entries(filters)
		.filter(([, value]) => value !== '')
		.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

/** Fingerprint of everything that determines a node's OUTPUT: its
 *  output-affecting config (not `label`, not export cosmetics) plus its
 *  incoming edges. */
export function nodeFingerprint(id: string, config: NodeConfig, edges: Edge[]): string {
	const preds = edges
		.filter((e) => e.target === id)
		.map((e) => `${e.source}#${e.targetHandle ?? ''}`)
		.sort();
	return JSON.stringify({
		preds,
		enabled: config.enabled,
		q: config.q,
		// A File can't be serialized — name+size+mtime identifies the upload.
		image: config.image
			? `${config.image.name}:${config.image.size}:${config.image.lastModified}`
			: null,
		where: config.where,
		filters: stableFilters(config.filters),
		mode: config.mode,
		n: config.n,
		rerank: config.rerank,
		minScore: config.minScore,
		refineScope: config.refineScope,
		combineMode: config.combineMode,
		tags: config.tags,
		atlas: config.capturedAtlasSelection ? config.capturedAtlasSelection.map(hitKey) : null,
	});
}
