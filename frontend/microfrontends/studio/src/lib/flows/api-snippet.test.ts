import { describe, expect, it } from 'vitest';
import { buildRunRequest, curlSnippet, pythonSnippet } from './api-snippet';

const NODES = [
	{ id: 'img', type: 'image' },
	{ id: 'txt', type: 'text' },
	{ id: 'm', type: 'model' },
];
const EDGES = [
	{ source: 'img', target: 'm' },
	{ source: 'txt', target: 'm' },
];
const CONFIG = { txt: { text: 'hej' }, m: { app: 'htr' } };

describe('buildRunRequest', () => {
	it("emits the service's RunRequest shape", () => {
		const { body } = buildRunRequest([{ id: 'txt', type: 'text' }], [], CONFIG);
		expect(body).toEqual({
			graph: { nodes: [{ id: 'txt', kind: 'text', config: { text: 'hej' } }], edges: [] },
			seeds: {},
		});
	});

	it('drops image nodes AND their edges, and reports which', () => {
		const { body, droppedImageNodes } = buildRunRequest(NODES, EDGES, CONFIG);
		// The upload lives in the browser; a server run that included it would always fail, so the
		// export is the server-runnable subset and says so.
		expect(droppedImageNodes).toEqual(['img']);
		expect(body.graph.nodes.map((n) => n.id)).toEqual(['txt', 'm']);
		expect(body.graph.edges).toEqual([{ source: 'txt', target: 'm' }]);
	});

	it('skips a node with no type rather than emitting a kindless one', () => {
		const { body } = buildRunRequest([{ id: 'x', type: undefined }], [], {});
		expect(body.graph.nodes).toEqual([]);
	});
});

describe('snippets', () => {
	it('curl posts valid JSON to the gateway row', () => {
		const { body } = buildRunRequest([{ id: 'txt', type: 'text' }], [], CONFIG);
		const out = curlSnippet(body);
		expect(out).toContain('/api/flows/runs');
		// The embedded body must round-trip — a snippet that pastes broken JSON is worse than none.
		const json = out.slice(out.indexOf("-d '") + 4, out.lastIndexOf("'"));
		expect(JSON.parse(json)).toEqual(body);
	});

	it('python uses httpx and the same path', () => {
		const { body } = buildRunRequest([{ id: 'txt', type: 'text' }], [], CONFIG);
		const out = pythonSnippet(body, 'http://gw:8888');
		expect(out).toContain('import httpx');
		expect(out).toContain('http://gw:8888/api/flows/runs');
	});
});
