import { describe, expect, it } from 'vitest';
import type { Edge } from '@xyflow/svelte';
import { nodeFingerprint } from './fingerprint';
import { DEFAULT_JSON_BODY, DEFAULT_PROMPT, type NodeConfig } from './types';

function cfg(patch: Partial<NodeConfig> = {}): NodeConfig {
	return {
		label: '',
		enabled: true,
		file: null,
		fileName: '',
		text: '',
		app: 'htrflow',
		requestName: '',
		modelPath: '',
		bodyMode: 'upstream',
		bodyTemplate: DEFAULT_JSON_BODY,
		promptTemplate: DEFAULT_PROMPT,
		datasetTable: '',
		mcpServer: '',
		mcpTool: '',
		mcpArgs: '{}',
		extractPath: '',
		regexPattern: '',
		regexReplace: '',
		regexMode: 'extract',
		...patch,
	};
}

const edge = (source: string, target: string): Edge => ({
	id: `${source}-${target}`,
	source,
	target,
});

describe('nodeFingerprint', () => {
	it('changes when output-affecting config changes', () => {
		const base = nodeFingerprint('n', cfg(), []);
		expect(nodeFingerprint('n', cfg({ text: 'x' }), [])).not.toBe(base);
		expect(nodeFingerprint('n', cfg({ app: 'transcribe' }), [])).not.toBe(base);
		expect(nodeFingerprint('n', cfg({ requestName: 'p1' }), [])).not.toBe(base);
		expect(nodeFingerprint('n', cfg({ enabled: false }), [])).not.toBe(base);
	});

	it('ignores the cosmetic label', () => {
		expect(nodeFingerprint('n', cfg({ label: 'pretty name' }), [])).toBe(
			nodeFingerprint('n', cfg(), []),
		);
	});

	it('changes when the incoming wiring changes, and only for the target', () => {
		const base = nodeFingerprint('n', cfg(), []);
		const wired = nodeFingerprint('n', cfg(), [edge('a', 'n')]);
		expect(wired).not.toBe(base);
		// An OUTGOING edge is not part of this node's output.
		expect(nodeFingerprint('n', cfg(), [edge('n', 'b')])).toBe(base);
	});

	it('is order-independent over incoming edges', () => {
		const ab = nodeFingerprint('n', cfg(), [edge('a', 'n'), edge('b', 'n')]);
		const ba = nodeFingerprint('n', cfg(), [edge('b', 'n'), edge('a', 'n')]);
		expect(ab).toBe(ba);
	});
});
