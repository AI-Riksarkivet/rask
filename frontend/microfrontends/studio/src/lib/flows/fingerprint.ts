/**
 * Output fingerprint for a flow node — a pure derivation over the node's
 * output-affecting config and incoming edges, kept out of the store class so
 * the invalidation rule is testable and readable in one place.
 *
 * Stored beside a node's cached output at compute time and compared at reuse
 * time: a mismatch means the node was edited or rewired since the output was
 * recorded, so the cache must not be served (this catches in-place `bind:`
 * config mutations that never pass through the store's setConfig).
 */
import type { Edge } from '@xyflow/svelte';
import type { NodeConfig } from './types';

/** Fingerprint of everything that determines a node's OUTPUT: its
 *  output-affecting config (not `label`) plus its incoming edges. */
export function nodeFingerprint(id: string, config: NodeConfig, edges: readonly Edge[]): string {
	const preds = edges
		.filter((e) => e.target === id)
		.map((e) => `${e.source}#${e.targetHandle ?? ''}`)
		.sort();
	return JSON.stringify({
		preds,
		enabled: config.enabled,
		// A File can't be serialized — name+size+mtime identifies the upload.
		file: config.file
			? `${config.file.name}:${config.file.size}:${config.file.lastModified}`
			: null,
		text: config.text,
		app: config.app,
		requestName: config.requestName,
		bodyMode: config.bodyMode,
		// Only when it is the mode in play — editing a template you are not sending must not
		// invalidate a perfectly good cached output.
		bodyTemplate: config.bodyMode === 'json' ? config.bodyTemplate : null,
		promptTemplate: config.promptTemplate,
		datasetTable: config.datasetTable,
		mcp: [config.mcpServer, config.mcpTool, config.mcpArgs],
		extractPath: config.extractPath,
		regex: [config.regexMode, config.regexPattern, config.regexReplace],
	});
}
