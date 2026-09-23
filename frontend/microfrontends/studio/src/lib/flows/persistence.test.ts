import { describe, expect, it } from 'vitest';
import { safeParseGraph } from './persistence';
import { NODE_KINDS } from './types';

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
						app: 'example',
						requestName: '',
					},
				},
			}),
		);
		expect(parsed?.nodes[0]?.type).toBe('model');
		expect(parsed?.config['model-1']?.app).toBe('example');
	});

	it('self-heals bad config fields instead of failing the parse', () => {
		const parsed = safeParseGraph(
			JSON.stringify({
				nodes: [NODE],
				config: { 'model-1': { label: 42, enabled: 'yes', app: null } },
			}),
		);
		// An unreadable app heals to UNSET rather than to a particular app: the Model node then asks
		// for a live Serve app instead of calling one the user never chose.
		expect(parsed?.config['model-1']).toMatchObject({ label: '', enabled: true, app: '' });
	});

	it('keeps a saved flow loadable as saved — every node kind, any app slug', () => {
		const nodes = NODE_KINDS.map((kind, i) => ({
			id: `${kind}-${i}`,
			type: kind,
			position: { x: i, y: 0 },
		}));
		const parsed = safeParseGraph(
			JSON.stringify({
				nodes,
				config: { 'model-4': { app: 'an-app-a-past-session-chose', requestName: 'n' } },
			}),
		);
		expect(parsed?.nodes.map((n) => n.type)).toEqual([...NODE_KINDS]);
		expect(parsed?.config['model-4']).toMatchObject({
			app: 'an-app-a-past-session-chose',
			requestName: 'n',
		});
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
