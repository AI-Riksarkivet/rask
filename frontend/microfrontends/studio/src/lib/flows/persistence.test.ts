import { describe, expect, it } from 'vitest';
import { safeParseGraph } from './persistence';

const NODE = { id: 'model-1', type: 'model', position: { x: 10, y: 20 } };

describe('safeParseGraph', () => {
	it('parses a valid snapshot', () => {
		const parsed = safeParseGraph(
			JSON.stringify({
				nodes: [NODE],
				edges: [{ id: 'e1', source: 'model-1', target: 'model-1' }],
				config: {
					'model-1': {
						label: 'M',
						enabled: true,
						fileName: '',
						text: '',
						app: 'htrflow',
						requestName: '',
					},
				},
			}),
		);
		expect(parsed?.nodes[0]?.type).toBe('model');
		expect(parsed?.config['model-1']?.app).toBe('htrflow');
	});

	it('self-heals bad config fields instead of failing the parse', () => {
		const parsed = safeParseGraph(
			JSON.stringify({
				nodes: [NODE],
				config: { 'model-1': { label: 42, enabled: 'yes', app: null } },
			}),
		);
		expect(parsed?.config['model-1']).toMatchObject({ label: '', enabled: true, app: 'htrflow' });
	});

	it('refuses a structurally-bad node (unknown kind) so the caller seeds', () => {
		const parsed = safeParseGraph(JSON.stringify({ nodes: [{ ...NODE, type: 'search' }] }));
		expect(parsed).toBeNull();
	});

	it('refuses an empty graph, non-JSON and null', () => {
		expect(safeParseGraph(JSON.stringify({ nodes: [] }))).toBeNull();
		expect(safeParseGraph('{oops')).toBeNull();
		expect(safeParseGraph(null)).toBeNull();
	});
});
