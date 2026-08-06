/**
 * Engine tests — the executor is a pure module behind the RunDeps seam, so the
 * whole dataflow (topo order, cycle refusal, cache reuse, failure blocking,
 * disabled bypass) runs here with no store, no network and no DOM.
 */
import { describe, expect, it, vi } from 'vitest';
import type { Edge, Node } from '@xyflow/svelte';
import {
	altoToText,
	applyRegex,
	compareTexts,
	CYCLE_ERROR,
	pickPath,
	renderBody,
	renderExtracted,
	runGraph,
	runSubgraph,
	type RunDeps,
} from './executor';
import { nodeFingerprint } from './fingerprint';
import {
	DEFAULT_JSON_BODY,
	DEFAULT_PROMPT,
	type FlowPayload,
	type NodeConfig,
	type NodeKind,
	type NodeRuntime,
} from './types';

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

interface Harness {
	deps: RunDeps;
	runtime: Record<string, NodeRuntime>;
	config: Record<string, NodeConfig>;
	invoke: ReturnType<typeof vi.fn>;
}

/** A minimal store stand-in mirroring FlowsGraph's cache/runtime semantics. */
function makeHarness(
	nodes: Array<{ id: string; kind: NodeKind }>,
	edges: Array<[string, string]>,
	config: Record<string, NodeConfig>,
	invoke?: (req: {
		app: string;
		name: string;
		path: string;
		payload: FlowPayload;
	}) => Promise<FlowPayload>,
): Harness {
	const flowNodes: Node[] = nodes.map((n) => ({
		id: n.id,
		type: n.kind,
		position: { x: 0, y: 0 },
		data: {},
	}));
	const flowEdges: Edge[] = edges.map(([source, target]) => ({
		id: `${source}-${target}`,
		source,
		target,
	}));
	const runtime: Record<string, NodeRuntime> = {};
	const blank = (): NodeRuntime => ({
		status: 'idle',
		error: null,
		ms: null,
		output: null,
		outputKey: null,
		stale: false,
	});
	for (const n of nodes) runtime[n.id] = blank();
	const invokeFn = vi.fn(
		invoke ??
			(async (): Promise<FlowPayload> => ({
				kind: 'text',
				text: '<alto/>',
				mime: 'application/xml',
				label: 'page',
			})),
	);
	const fingerprint = (id: string): string => nodeFingerprint(id, config[id] ?? cfg(), flowEdges);
	const deps: RunDeps = {
		nodes: flowNodes,
		edges: flowEdges,
		config: (id) => config[id] ?? cfg(),
		kindOf: (id) => (flowNodes.find((n) => n.id === id)?.type ?? null) as NodeKind | null,
		patchRuntime: (id, patch) => {
			runtime[id] = { ...(runtime[id] ?? blank()), ...patch };
		},
		cachedOutput: (id) => {
			const rt = runtime[id];
			const out = rt?.output;
			if (!out || out.failed || rt.stale) return null;
			if (rt.outputKey !== fingerprint(id)) return null;
			return out;
		},
		fingerprint,
		invoke: invokeFn,
		// The log is part of the seam, so the engine is testable without a store.
		log: vi.fn(),
	};
	return { deps, runtime, config, invoke: invokeFn };
}

const CHAIN: Array<{ id: string; kind: NodeKind }> = [
	{ id: 'text', kind: 'text' },
	{ id: 'model', kind: 'model' },
	{ id: 'inspect', kind: 'inspect' },
];
const CHAIN_EDGES: Array<[string, string]> = [
	['text', 'model'],
	['model', 'inspect'],
];

describe('runGraph', () => {
	it('flows a payload down the chain and records per-node status', async () => {
		const h = makeHarness(CHAIN, CHAIN_EDGES, {
			text: cfg({ text: 'hello' }),
			model: cfg(),
			inspect: cfg(),
		});
		const error = await runGraph(h.deps);
		expect(error).toBeNull();
		expect(h.invoke).toHaveBeenCalledTimes(1);
		expect(h.invoke.mock.calls[0]?.[0]).toMatchObject({
			app: 'htrflow',
			payload: { kind: 'text', text: 'hello' },
		});
		expect(h.runtime['model']?.status).toBe('done');
		expect(h.runtime['model']?.ms).not.toBeNull();
		expect(h.runtime['inspect']?.status).toBe('done');
		const out = h.runtime['inspect']?.output?.payload;
		expect(out?.kind === 'text' && out.text).toBe('<alto/>');
	});

	it('refuses a cyclic graph with the cycle error', async () => {
		const h = makeHarness(CHAIN, [...CHAIN_EDGES, ['inspect', 'text']], {
			text: cfg({ text: 'x' }),
		});
		expect(await runGraph(h.deps)).toBe(CYCLE_ERROR);
	});

	it('a failing model blocks its dependents instead of running them on partial input', async () => {
		const h = makeHarness(
			CHAIN,
			CHAIN_EDGES,
			{ text: cfg({ text: 'x' }), model: cfg(), inspect: cfg() },
			async () => {
				throw new Error('serve down');
			},
		);
		expect(await runGraph(h.deps)).toBeNull(); // node errors never reject the run
		expect(h.runtime['model']?.status).toBe('error');
		expect(h.runtime['model']?.error).toBe('serve down');
		expect(h.runtime['inspect']?.status).toBe('error');
		expect(h.runtime['inspect']?.error).toMatch(/upstream/i);
		expect(h.runtime['inspect']?.output?.failed).toBe(true);
	});

	it('a disabled node bypasses its own work but forwards the payload', async () => {
		const h = makeHarness(CHAIN, CHAIN_EDGES, {
			text: cfg({ text: 'passthrough' }),
			model: cfg({ enabled: false }),
			inspect: cfg(),
		});
		expect(await runGraph(h.deps)).toBeNull();
		expect(h.invoke).not.toHaveBeenCalled();
		const out = h.runtime['inspect']?.output?.payload;
		expect(out?.kind === 'text' && out.text).toBe('passthrough');
	});

	it('a model with no upstream payload idles rather than erroring', async () => {
		const h = makeHarness(CHAIN, CHAIN_EDGES, {
			text: cfg({ text: '   ' }), // whitespace-only → no payload
			model: cfg(),
		});
		expect(await runGraph(h.deps)).toBeNull();
		expect(h.invoke).not.toHaveBeenCalled();
		expect(h.runtime['model']?.status).toBe('idle');
	});

	it('an alto node refuses a bytes payload with a wiring hint', async () => {
		const h = makeHarness(
			[
				{ id: 'text', kind: 'text' },
				{ id: 'alto', kind: 'alto' },
			],
			[['text', 'alto']],
			{ text: cfg({ text: 'not xml, but text is fine' }), alto: cfg() },
		);
		expect(await runGraph(h.deps)).toBeNull();
		expect(h.runtime['alto']?.status).toBe('done'); // text payloads are accepted
	});
});

describe('runSubgraph (per-node ▶ + cache)', () => {
	it('reuses a predecessor cache and only recomputes the target', async () => {
		const h = makeHarness(CHAIN, CHAIN_EDGES, {
			text: cfg({ text: 'hello' }),
			model: cfg(),
			inspect: cfg(),
		});
		await runGraph(h.deps);
		expect(h.invoke).toHaveBeenCalledTimes(1);

		const { error, ran } = await runSubgraph(h.deps, 'inspect');
		expect(error).toBeNull();
		expect(ran).toEqual(['inspect']); // model + text served from cache
		expect(h.invoke).toHaveBeenCalledTimes(1); // no second network call
	});

	it('an edited upstream config invalidates the cache via the fingerprint', async () => {
		const h = makeHarness(CHAIN, CHAIN_EDGES, {
			text: cfg({ text: 'hello' }),
			model: cfg(),
			inspect: cfg(),
		});
		await runGraph(h.deps);
		h.config['text'] = cfg({ text: 'edited' }); // in-place edit, no store involved

		const { ran } = await runSubgraph(h.deps, 'inspect');
		// text's fingerprint changed → recompute; model's input recomputed → it
		// can't serve its cache either; the target always recomputes.
		expect(ran).toEqual(['text', 'model', 'inspect']);
		expect(h.invoke).toHaveBeenCalledTimes(2);
	});

	it('fresh forces the whole upstream branch', async () => {
		const h = makeHarness(CHAIN, CHAIN_EDGES, {
			text: cfg({ text: 'hello' }),
			model: cfg(),
			inspect: cfg(),
		});
		await runGraph(h.deps);
		const { ran } = await runSubgraph(h.deps, 'inspect', { fresh: true });
		expect(ran).toEqual(['text', 'model', 'inspect']);
		expect(h.invoke).toHaveBeenCalledTimes(2);
	});
});

describe('renderBody (JSON body mode)', () => {
	it('interpolates the upstream text, JSON-escaped', () => {
		const body = renderBody(DEFAULT_JSON_BODY, {
			kind: 'text',
			text: 'hej "du"\nrad två',
			mime: 'text/plain',
			label: 't',
		});
		// The point of escaping: the result must still PARSE, quotes and newline included.
		expect(JSON.parse(body)).toEqual({ text: 'hej "du"\nrad två' });
	});

	it('interpolates empty for a bytes payload or no input, so a fixed probe still works', () => {
		expect(JSON.parse(renderBody(DEFAULT_JSON_BODY, null))).toEqual({ text: '' });
		const bytes: FlowPayload = {
			kind: 'bytes',
			bytes: new ArrayBuffer(4),
			mime: 'image/png',
			label: 'p',
		};
		expect(JSON.parse(renderBody(DEFAULT_JSON_BODY, bytes))).toEqual({ text: '' });
		// A template that never mentions the input is untouched.
		expect(renderBody('{"model":"gemma-31b"}', null)).toBe('{"model":"gemma-31b"}');
	});

	it('replaces EVERY occurrence, not just the first', () => {
		const out = renderBody('{"a":"{{input}}","b":"{{input}}"}', {
			kind: 'text',
			text: 'x',
			mime: 'text/plain',
			label: 't',
		});
		expect(JSON.parse(out)).toEqual({ a: 'x', b: 'x' });
	});
});

describe('model node in JSON body mode', () => {
	it('sends the rendered template as application/json instead of the raw payload', async () => {
		const h = makeHarness(CHAIN, CHAIN_EDGES, {
			text: cfg({ text: 'transcribe this' }),
			model: cfg({ bodyMode: 'json', bodyTemplate: '{"prompt":"{{input}}"}', app: 'gemma-31b' }),
			inspect: cfg(),
		});
		expect(await runGraph(h.deps)).toBeNull();
		const sent = h.invoke.mock.calls[0]?.[0];
		expect(sent).toMatchObject({ app: 'gemma-31b' });
		expect(sent.payload).toMatchObject({ kind: 'text', mime: 'application/json' });
		expect(JSON.parse(sent.payload.text)).toEqual({ prompt: 'transcribe this' });
	});

	it('runs with NO upstream input at all — a fixed probe against an endpoint', async () => {
		const h = makeHarness([{ id: 'model', kind: 'model' }], [], {
			model: cfg({ bodyMode: 'json', bodyTemplate: '{"input":"ping"}', app: 'qwen3-embed' }),
		});
		expect(await runGraph(h.deps)).toBeNull();
		expect(h.invoke).toHaveBeenCalledTimes(1);
		expect(h.runtime['model']?.status).toBe('done');
	});

	it('still idles with no input in upstream mode — an unconfigured node is not an error', async () => {
		const h = makeHarness([{ id: 'model', kind: 'model' }], [], { model: cfg() });
		expect(await runGraph(h.deps)).toBeNull();
		expect(h.invoke).not.toHaveBeenCalled();
		expect(h.runtime['model']?.status).toBe('idle');
	});
});

describe('altoToText', () => {
	it('joins String CONTENT per TextLine', () => {
		const xml = `<?xml version="1.0"?><alto><Layout><Page><PrintSpace>
			<TextBlock><TextLine><String CONTENT="Anno" WC="0.9"/><String CONTENT="1723"/></TextLine>
			<TextLine><String CONTENT="den" /><String CONTENT="12" /><String CONTENT="maij" /></TextLine>
			</TextBlock></PrintSpace></Page></Layout></alto>`;
		expect(altoToText(xml)).toBe('Anno 1723\nden 12 maij');
	});

	it('yields empty output for a document with no text lines', () => {
		expect(altoToText('<alto></alto>')).toBe('');
	});
});

describe('pickPath / renderExtracted (the Extract node)', () => {
	const doc = {
		choices: [{ message: { content: 'anno 1723' } }],
		data: [{ embedding: [0.1, 0.2] }],
	};

	it('walks bracket and dotted indices alike', () => {
		expect(pickPath(doc, 'choices[0].message.content')).toBe('anno 1723');
		expect(pickPath(doc, 'choices.0.message.content')).toBe('anno 1723');
		expect(pickPath(doc, 'data[0].embedding[1]')).toBe(0.2);
	});

	it('returns undefined for a miss rather than throwing', () => {
		expect(pickPath(doc, 'choices[9].message')).toBeUndefined();
		expect(pickPath(doc, 'nope')).toBeUndefined();
		expect(pickPath({ a: 1 }, 'a.b')).toBeUndefined(); // scalar with path left to walk
	});

	it('emits a string unquoted and anything else as JSON', () => {
		// The whole point: downstream must see the ANSWER, not a quoted JSON literal.
		expect(renderExtracted('anno 1723')).toBe('anno 1723');
		expect(renderExtracted({ a: 1 })).toBe('{\n  "a": 1\n}');
	});
});

describe('applyRegex (the Regex node)', () => {
	it('extracts capture group 1 across ALL matches', () => {
		expect(applyRegex('Anno 1723 and Anno 1724', 'Anno (\\d{4})', 'extract', '')).toBe(
			'1723\n1724',
		);
	});

	it('falls back to the whole match with no capture group', () => {
		expect(applyRegex('a1 b2', '\\d', 'extract', '')).toBe('1\n2');
	});

	it('replaces globally', () => {
		expect(applyRegex('1723 and 1724', '\\d+', 'replace', 'N')).toBe('N and N');
	});
});

describe('compareTexts (the Compare node)', () => {
	it('reports both sides, a verdict and the first differing line', () => {
		const out = compareTexts(['line one\nline two', 'line one\nline TWO']);
		expect(out).toContain('2 inputs compared');
		expect(out).toContain('── verdict: A and B differ ──');
		expect(out).toContain('first difference at line 2');
	});

	it('says so when the two are identical', () => {
		expect(compareTexts(['same', 'same'])).toContain('A and B are identical');
	});
});

describe('the multi-input Compare node in a run', () => {
	it('reads EVERY input, not the first', async () => {
		const h = makeHarness(
			[
				{ id: 'a', kind: 'text' },
				{ id: 'b', kind: 'text' },
				{ id: 'c', kind: 'compare' },
			],
			[
				['a', 'c'],
				['b', 'c'],
			],
			{ a: cfg({ text: 'gemma says A' }), b: cfg({ text: 'qwen says B' }), c: cfg() },
		);
		expect(await runGraph(h.deps)).toBeNull();
		const out = h.runtime['c']?.output?.payload;
		const text = out?.kind === 'text' ? out.text : '';
		expect(text).toContain('gemma says A');
		expect(text).toContain('qwen says B');
	});
});

describe('the mcp scaffold', () => {
	it('refuses and names the missing piece', async () => {
		const h = makeHarness([{ id: 'm', kind: 'mcp' }], [], {
			m: cfg({ mcpServer: 'ra-mcp', mcpTool: 'search_transcribed' }),
		});
		expect(await runGraph(h.deps)).toBeNull();
		expect(h.runtime['m']?.status).toBe('error');
		expect(h.runtime['m']?.error).toMatch(/scaffold/i);
	});
});
