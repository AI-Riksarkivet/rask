<script lang="ts" module>
	import AccessGraphNode, { type AccessNodeType } from './AccessGraphNode.svelte';
	import type { NodeTypes } from '@xyflow/svelte';

	// svelte-flow rule: register node components ONCE at module scope, never inline.
	const nodeTypes: NodeTypes = { access: AccessGraphNode };
</script>

<script lang="ts">
	// The whole view. One canvas that stays mounted, a query bar that highlights it, a facet rail that
	// narrows it, and an inspector beside it — replacing four tabs that shared no state, no URL and no
	// question. Switching "tabs" used to throw away the graph you were looking at; nothing here does.
	import { type Edge, MarkerType } from '@xyflow/svelte';
	import { GraphCanvas } from '@rask/flow';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import * as Popover from '@rask/ui/popover';
	import { goto } from '$app/navigation';
	import { onMount, untrack } from 'svelte';
	import { page } from '$app/state';
	import { Filter, RotateCcw, ShieldAlert } from '@lucide/svelte';
	import { parseModelTypes, type Tuple } from '../access';
	import {
		accessMe,
		checkAccess,
		expand,
		fetchAccessModel,
		fetchStore,
		fetchTables,
		listObjects,
		listUsers,
	} from '../remote/access.remote';
	import { controlTick, liveRead } from '$lib/live/tick.svelte';
	import FacetRail from './FacetRail.svelte';
	import Inspector from './Inspector.svelte';
	import QueryBar from './QueryBar.svelte';
	import {
		applyFacets,
		type BuiltGraph,
		buildNeighbourhood,
		buildWholeGraph,
		idType,
		isSubject,
		layout,
		overlay,
		type QueryKind,
		walkDerivation,
	} from './graph';
	import { buildModelGraph } from './model-graph';
	import { clearHistory, type HistoryEntry, loadHistory, recordQuery } from './history';

	const EMPTY: BuiltGraph = {
		nodes: [],
		edges: [],
		typeCounts: new Map(),
		relationCounts: new Map(),
	};

	// ── URL is the state: a view must be linkable, and back/forward must work ──
	let kind = $state<QueryKind>('why');
	let user = $state('');
	let relation = $state('');
	let object = $state('');
	let objectType = $state('table');
	let depth = $state(3);
	let seed = $state('');
	let selectedTypes = $state(new Set<string>());
	let selectedRelations = $state(new Set<string>());
	/** `data` = the live store (who has what NOW). `model` = the schema (what CAN grant what, ever).
	 *  A toggle beside the canvas, not a tab: the tabs are dead and stay dead, and switching must not
	 *  throw away the query you were reading. */
	let view = $state<'data' | 'model'>('data');
	/** Recent queries. The URL makes ONE query shareable; this makes the SEQUENCE retraceable, which
	 *  `replaceState` (used so the address bar does not fill with keystrokes) otherwise destroys. */
	let history = $state<HistoryEntry[]>([]);

	// The exact query string this component last wrote. Hydration compares against it so our own
	// replaceState does not bounce back into the fields mid-typing, while a real history navigation
	// (back/forward, or a pasted link) still does. An earlier version stamped a synthetic `_w` token
	// into the URL to achieve this — which worked, but put a meaningless parameter in every link the
	// view exists to make shareable.
	//
	// NOT `$state`, and that is the whole point. The hydration effect READS this, so making it reactive
	// makes the effect depend on it — and `pushUrl` assigns it, which re-fired the effect synchronously,
	// BEFORE `goto` had updated `page.url`. The effect then compared the OLD query string against the
	// NEW token, concluded the URL was foreign, and hydrated defaults straight over the user's query.
	// The symptom was a facet click silently resetting the whole form. Plain bookkeeping, plain `let`.
	let lastPushed: string | null = null;

	const activeFacets = $derived(selectedTypes.size > 0 || selectedRelations.size > 0);

	const params = $derived(page.url.searchParams);

	// This one IS correctly an $effect, despite assigning state: it syncs an EXTERNAL source (history
	// navigation) into fields the user also types into. `$derived` cannot express that — a derived
	// value has one writer, and these have two.
	//
	// The body is `untrack`ed, and that is load-bearing rather than tidiness. Without it the effect
	// READS the very state it just wrote — `seed` in the guard below, and `kind`/`user`/… inside
	// `run()` — which makes it its own dependency and re-runs it. The visible symptom was a pasted
	// link never reproducing its answer: the re-entrant effect clobbered the in-flight query.
	// (Same idiom the zone's other estate-gated readers use, for the same reason.)
	$effect(() => {
		const q = params;
		if (q.toString() === lastPushed) return;
		untrack(() => {
			kind = (q.get('q') as QueryKind | null) ?? 'why';
			user = q.get('user') ?? '';
			relation = q.get('relation') ?? '';
			object = q.get('object') ?? '';
			objectType = q.get('type') ?? 'table';
			depth = Number(q.get('depth') ?? '3') || 3;
			seed = q.get('seed') ?? '';
			selectedTypes = new Set((q.get('types') ?? '').split(',').filter(Boolean));
			selectedRelations = new Set((q.get('rels') ?? '').split(',').filter(Boolean));
			view = q.get('view') === 'model' ? 'model' : 'data';
			if (seed) void loadNeighbourhood(seed);
			if (q.get('run') === '1') void run();
		});
	});

	function pushUrl(extra: Record<string, string>): void {
		const next = new URLSearchParams();
		const set = (key: string, value: string) => {
			if (value) next.set(key, value);
		};
		set('q', kind);
		set('user', user.trim());
		set('relation', relation.trim());
		set('object', object.trim());
		set('type', objectType.trim());
		set('depth', String(depth));
		set('seed', seed);
		set('types', [...selectedTypes].join(','));
		set('rels', [...selectedRelations].join(','));
		set('view', view === 'model' ? 'model' : '');
		for (const [key, value] of Object.entries(extra)) set(key, value);
		lastPushed = next.toString();
		void goto(`?${lastPushed}`, { replaceState: true, keepFocus: true, noScroll: true });
	}

	// ── data ──
	let neighbourhood = $state<BuiltGraph | null>(null);
	let answer = $state<BuiltGraph | null>(null);
	let verdict = $state<{ allowed: boolean; user: string; relation: string; object: string } | null>(
		null,
	);
	let chain = $state<string[]>([]);
	let busy = $state(false);
	let failure = $state<string | null>(null);
	let denied = $state(false);
	let selected = $state<string | null>(null);
	let dsl = $state<string | null>(null);
	let modelTypes = $state<string[]>([]);
	/** `{condition: {parameter: type}}` served by GET /v1/access/model — the grant dialog renders TYPED
	 *  inputs from this rather than from a copy that would drift the moment the model gains a parameter. */
	let modelConditions = $state<Record<string, Record<string, string>>>({});
	let registry = $state<string[]>([]);
	/** Every tuple in the store — the canvas's base graph, and what the facet counts are computed on. */
	let storeTuples = $state<Tuple[]>([]);
	/** The store is bigger than MAX_GRAPH_PAGES. Said out loud rather than silently clipped. */
	let truncated = $state(false);
	/**
	 * The signed-in identity. Load-bearing, not decoration: the OIDC `sub` here is what the tuple store
	 * actually keys on, and under Dex that is an opaque base64 blob like `CiQwOGE4Njg0Yi1kYjg4…`. Nobody
	 * can type that, and typing the obvious `user:alice` returns a CORRECT denial that reads like a bug.
	 * So the subject field is seeded from this, and a "me" chip puts it back one click away.
	 */
	let me = $state<{ sub: string; name: string | null; estate_admin: boolean } | null>(null);

	// onMount, not $effect: these two fetch once and depend on nothing reactive. An $effect with no
	// dependencies happens to run once too, which is exactly why it is the wrong tool — it states
	// "re-run when something changes" about code that has nothing to re-run for.
	onMount(() => {
		history = loadHistory();
		void loadModel();
		void loadMe();
	});

	// The resting graph is LIVE: read once on mount (liveRead's unconditional first read), then again
	// whenever the CONTROL cursor advances — a grant landing anywhere on the estate (another admin, the
	// per-object grant API, a bootstrap job) redraws this canvas without a timer. The registry rides the
	// same tick because table/namespace mutations move the same cursor. The model does not: model
	// changes are code migrations, not runtime events, so no cursor can report one.
	liveRead(controlTick, () => {
		void loadWholeGraph();
		void loadRegistry();
	});

	async function loadMe(): Promise<void> {
		// Fail-soft to null (no session, catalog down): the "me" chip simply does not render.
		const parsed = await accessMe();
		if (!parsed) return;
		me = { sub: parsed.sub, name: parsed.name, estate_admin: parsed.estate_admin };
		// Seed the subject only if the user has not already typed or linked one.
		if (!user.trim()) user = `user:${parsed.sub}`;
	}

	async function loadModel(): Promise<void> {
		const res = await fetchAccessModel();
		if (!res.ok) {
			denied = res.status === 401 || res.status === 403;
			return;
		}
		dsl = res.data.dsl;
		modelTypes = parseModelTypes(res.data.dsl);
		modelConditions = res.data.conditions;
	}

	async function loadRegistry(): Promise<void> {
		const res = await fetchTables();
		if (res.ok) registry = [...res.data.tables].sort();
	}

	/** Relations the model defines on the type currently in play — the query bar's suggestions, so a
	 *  phantom relation is a keystroke away from impossible rather than a 400 one round trip later. */
	const relationOptions = $derived.by(() => {
		if (!dsl) return [];
		const target = kind === 'what' ? objectType : idType(object || seed || 'table');
		const lines = dsl.split('\n');
		const start = lines.findIndex((line) => new RegExp(`^\\s*type\\s+${target}\\s*$`).test(line));
		if (start < 0) return [];
		const out: string[] = [];
		for (const line of lines.slice(start + 1)) {
			if (/^\s*type\s+\S+\s*$/.test(line)) break;
			const match = /^\s*define\s+([A-Za-z0-9_]+)\s*:/.exec(line);
			if (match?.[1]) out.push(match[1]);
		}
		return out;
	});

	/**
	 * The relations a grant on the SELECTED object may name — the model's directly-assignable ones for
	 * that type. Parsed from the DSL: a `define x: [ ... ]` line declares direct assignment, whereas a
	 * `define x: y or z` line is derived and the catalog refuses to write it. Without this the dialog
	 * was a free-text box that taught the rule via a 400 one round trip later.
	 */
	const assignableForSelected = $derived.by(() => {
		if (!dsl || !selected) return [];
		const target = idType(selected);
		const lines = dsl.split('\n');
		const start = lines.findIndex((line) => new RegExp(`^\\s*type\\s+${target}\\s*$`).test(line));
		if (start < 0) return [];
		const out: string[] = [];
		for (const line of lines.slice(start + 1)) {
			if (/^\s*type\s+\S+\s*$/.test(line)) break;
			const match = /^\s*define\s+([A-Za-z0-9_]+)\s*:\s*\[/.exec(line);
			if (match?.[1]) out.push(match[1]);
		}
		return out;
	});

	/** The handful of ids worth offering as one-click seeds: the estate root, every namespace, every
	 *  registered table. Derived from the LIVE store and registry, so it is never a hardcoded guess. */
	const entryPoints = $derived.by(() => {
		const fromStore = [...new Set(storeTuples.flatMap((t) => [t.user, t.object]))]
			.filter((id) => !isSubject(id))
			.sort();
		const fromRegistry = registry.map((t) => `table:${t}`);
		return [...new Set([...fromStore, ...fromRegistry])].slice(0, 12);
	});

	const objectOptions = $derived([
		...registry.map((t) => `table:${t}`),
		...[...new Set(registry.map((t) => (t.includes('$') ? t.slice(0, t.indexOf('$')) : t)))].map(
			(ns) => `namespace:${ns}`,
		),
	]);

	/**
	 * Load the WHOLE tuple store as the base graph.
	 *
	 * This is the view's centre of gravity, and an earlier version got it backwards: the canvas began
	 * empty and a query ADDED a handful of nodes, so "filter down" had nothing to filter and the first
	 * thing anyone saw was an empty box with a free-text field. The whole point is the opposite — show
	 * every relationship, then let a query highlight within it and the facets narrow it.
	 *
	 * The page loop lives in `fetchStore` on the zone server now — one browser round trip instead of
	 * forty — with the same bounded ceiling: a store larger than it is reported as truncated rather
	 * than silently clipped, because a graph that quietly omits half the estate is worse than one that
	 * admits it.
	 */
	async function loadWholeGraph(): Promise<void> {
		const res = await fetchStore();
		if (!res.ok) {
			denied = res.status === 401 || res.status === 403;
			if (!denied) failure = res.detail;
			return;
		}
		storeTuples = res.data.tuples;
		truncated = res.data.truncated;
		// `focus` on nothing: the whole store has no single centre, so every node is ordinary until a
		// query names one. buildWholeGraph lays subjects left and objects right off the tuple direction.
		neighbourhood = buildWholeGraph(res.data.tuples);
	}

	/** One object's own tuples, for the inspector — the canvas already holds the whole store. */
	async function loadNeighbourhood(target: string): Promise<void> {
		if (!target) return;
		await loadWholeGraph();
	}

	function fail(res: { status: number; detail: string }): void {
		denied = res.status === 401 || res.status === 403;
		failure = denied ? 'This surface is estate-admin only.' : res.detail;
	}

	async function run(): Promise<void> {
		if (busy) return;
		busy = true;
		failure = null;
		verdict = null;
		chain = [];
		answer = null;
		try {
			if (kind === 'what') await runWhat();
			else if (kind === 'who') await runWho();
			else await runWhy();
		} finally {
			busy = false;
		}
	}

	async function runWhat(): Promise<void> {
		const res = await listObjects({
			user: user.trim(),
			relation: relation.trim(),
			type: objectType.trim(),
		});
		if (!res.ok) {
			fail(res);
			return;
		}
		const parsed = res.data;
		answer = buildNeighbourhood(
			parsed.user,
			parsed.objects.map((o) => ({ user: parsed.user, relation: parsed.relation, object: o })),
		);
		seed = parsed.user;
		selected = parsed.user;
		chain = [
			`${parsed.user} holds ${parsed.relation} on ${parsed.objects.length} ${parsed.type} object(s)`,
		];
	}

	async function runWho(): Promise<void> {
		const res = await listUsers({ object: object.trim(), relation: relation.trim() });
		if (!res.ok) {
			fail(res);
			return;
		}
		const parsed = res.data;
		answer = buildNeighbourhood(
			parsed.object,
			parsed.users.map((u) => ({ user: u, relation: parsed.relation, object: parsed.object })),
		);
		seed = parsed.object;
		selected = parsed.object;
		chain = [
			`${parsed.users.length} subject(s) hold ${parsed.relation} on ${parsed.object}` +
				(parsed.truncated ? ' — TRUNCATED by the store, the real number is higher' : ''),
		];
	}

	async function runWhy(): Promise<void> {
		// Check AND Expand together: the verdict is the fact, the tree is the explanation. Shipping
		// either alone is exactly what made the old Check tab a dead end.
		const triple = { user: user.trim(), relation: relation.trim(), object: object.trim() };
		const [checkRes, expandRes] = await Promise.all([
			checkAccess(triple),
			expand({ object: triple.object, relation: triple.relation, depth }),
		]);
		if (!checkRes.ok) {
			fail(checkRes);
			return;
		}
		const probed = checkRes.data;
		verdict = {
			allowed: probed.allowed,
			user: probed.checked.user,
			relation: probed.checked.relation,
			object: probed.checked.object,
		};
		selected = probed.checked.object;
		if (!expandRes.ok) {
			failure = 'The verdict is live, but the derivation could not be read.';
			return;
		}
		const parsed = expandRes.data;
		answer = walkDerivation(parsed.tree, parsed.object, parsed.relation);
		seed = parsed.object;
		chain = answer.edges
			.filter((e) => e.onPath)
			.map((e) => `${e.source} → ${e.target} (${e.label})`);
	}

	// ── the rendered graph ──
	/** The schema graph, derived from the DSL the model endpoint already serves. Rebuilt only when the
	 *  DSL changes — it is a pure function of the model and owes nothing to the store. */
	const modelGraph = $derived(dsl ? buildModelGraph(dsl) : EMPTY);

	const composed = $derived(
		view === 'model' ? modelGraph : overlay(neighbourhood ?? EMPTY, answer),
	);
	const filtered = $derived(
		applyFacets(composed, { types: selectedTypes, relations: selectedRelations }),
	);

	/**
	 * The edges the pointer's subject touches — a node hover means "its edges", an edge hover means
	 * exactly that one.
	 *
	 * Hover is PURE ADDITION, by ruling: the touched edges get a subtle primary tint and nothing else
	 * on the canvas changes. An earlier version dimmed everything unrelated to 8% opacity — "highlight
	 * by darkening the world" — which read as the UI breaking every time the pointer crossed the
	 * graph, and a missed pointerleave (fast exits over SVG edges lose the event) left the whole
	 * canvas stuck dark. Emphasis that only adds has nothing to get stuck: releasing the pointer just
	 * loses a tint, and the container-level reset below clears it even when the per-element leave
	 * event never fires.
	 */
	let hovered = $state<string | null>(null);
	/** Hovering an EDGE, not a node. A node answers "what does this touch"; an edge answers "what does
	 *  this one grant connect" — on a dense canvas those are different questions, and only the first
	 *  was answerable. One tuple IS an edge, so it deserves the same emphasis its endpoints get. */
	let hoveredEdge = $state<string | null>(null);
	const touchedEdges = $derived.by(() => {
		if (hoveredEdge) return new Set([hoveredEdge]);
		if (!hovered) return null;
		const edgeIds = new Set<string>();
		for (const e of filtered.edges) {
			if (e.source === hovered || e.target === hovered) edgeIds.add(e.id);
		}
		return edgeIds;
	});

	/** The node the last "Jump to" chip named — the canvas glides its viewport to it, so the chip has a
	 *  visible consequence beyond prefilled form fields. A fresh object per click, so clicking the same
	 *  chip again re-centres (see FlowFocus). */
	let focusTarget = $state<{ id: string } | null>(null);

	let nodes = $state.raw<AccessNodeType[]>([]);
	let edges = $state.raw<Edge[]>([]);

	/**
	 * Layout in its OWN derivation, so it re-runs only when the graph actually changes.
	 *
	 * It used to live inside the node-build effect below, which also read the hover state. That made
	 * every pointer enter/leave re-run the full barycentre pass and re-diff the whole canvas:
	 * O(nodes+edges) work per mouse movement, on a canvas capped at 4000 tuples. The placer runs when
	 * facets, the query, or the view flip — and since hover stopped touching nodes at all, pointer
	 * movement now re-maps only the edge array.
	 */
	const positioned = $derived(layout(filtered.nodes, filtered.edges));

	// Nodes owe NOTHING to hover: roles come from the query and the facets alone — `dim` means
	// "filtered out or off-query", exactly what the legend says, and never "the pointer is elsewhere".
	$effect(() => {
		nodes = positioned.map((n) => ({
			id: n.id,
			type: 'access' as const,
			position: { x: n.x, y: n.y },
			data: {
				fgaId: n.id,
				fgaType: n.fgaType,
				label: n.label,
				role: n.role,
				via: n.via,
			},
		}));
	});

	$effect(() => {
		const touched = touchedEdges;
		// Every edge gets an ARROWHEAD. A tuple is directional — `user:alice --owner--> table:x` — and an
		// undirected line in an authorization graph is not a simplification, it is the wrong picture:
		// "alice owns the table" and "the table owns alice" would render identically.
		//
		// The lit path is coloured EXPLICITLY here rather than through `--xy-edge-stroke-selected`. That
		// variable looks like the right lever and is not: the vendor stylesheet scopes it to
		// `.svelte-flow__edge.selected` / `:focus`, i.e. USER selection, which this never sets. Driving
		// the highlight through it meant the derivation was distinguished only by animation and opacity,
		// with a lit hop and a dimmed one sharing a stroke colour. `--primary`/`--muted-foreground` are
		// token `var()`s, which resolve inside inline SVG styles and marker defs, so this stays on-token
		// and flips with the theme.
		edges = filtered.edges.map((e) => ({
			id: e.id,
			source: e.source,
			target: e.target,
			label: e.condition ? `${e.label} ⏳` : e.label,
			animated: e.onPath,
			markerEnd: {
				type: MarkerType.ArrowClosed,
				width: 18,
				height: 18,
				// The arrowhead wears the same colour as its line — a green grant ending in a grey
				// arrow reads as two different edges meeting.
				color: e.onPath ? 'var(--primary)' : restingStroke(e.kind),
			},
			style: edgeStyle(e, touched?.has(e.id) ?? false),
		}));
	});

	/**
	 * The stored tuples the current derivation rests on — the store's own facts, restricted to the
	 * objects the answer actually touches.
	 *
	 * Both endpoints must be derivation nodes, so this is the sub-graph of the store INDUCED by the
	 * answer rather than everything adjacent to it: the minimal set of real facts that reproduces the
	 * verdict. Taken from `storeTuples` and never from the derivation's own edges — a derivation edge
	 * can name a computed permission (`can_read_data`), which is not directly assignable and would be
	 * rejected outright as a fixture tuple.
	 */
	const supportingTuples = $derived.by(() => {
		if (!answer) return [];
		const inAnswer = new Set(answer.nodes.map((n) => n.id));
		return storeTuples.filter((t) => inAnswer.has(t.user) && inAnswer.has(t.object));
	});

	/** Resting stroke per edge KIND — colour is the code (the legend names it): grants green,
	 *  role/team membership amber, `parent` scaffolding muted. Uncoded edges (the model view) keep
	 *  the muted look. */
	const KIND_STROKE: Record<string, string> = {
		grant: 'var(--success)',
		membership: 'var(--warning)',
		structural: 'var(--muted-foreground)',
	};
	const restingStroke = (kind: string | undefined): string =>
		KIND_STROKE[kind ?? 'structural'] ?? 'var(--muted-foreground)';

	/** Edge chrome: the query's lit path owns "lit" outright; hover ADDS a subtle primary tint to the
	 *  touched edges and takes nothing from the rest — there is no dimming pass, so releasing the
	 *  pointer has nothing to restore and a lost leave event has nothing to leave stuck. */
	function edgeStyle(
		e: { onPath: boolean; kind?: string; condition?: string | null },
		touched: boolean,
	): string {
		// A time-boxed grant is DASHED wherever it appears — "this expires" must stay legible whether
		// the edge is resting, tinted, or lit.
		const dash = e.condition ? ' stroke-dasharray: 6 4;' : '';
		if (e.onPath) return `stroke: var(--primary); stroke-width: 2;${dash}`;
		if (touched) return `stroke: var(--primary); stroke-width: 1.75; opacity: 0.75;${dash}`;
		const faint = e.kind === 'structural' || e.kind === undefined ? 0.35 : 0.5;
		return `stroke: ${restingStroke(e.kind)}; opacity: ${faint};${dash}`;
	}

	/** Is anything narrowing the view right now? Drives whether Clear is offered at all — an always-on
	 *  reset next to Run reads as a second action rather than an escape hatch. */
	const hasQuery = $derived(
		answer !== null ||
			verdict !== null ||
			selected !== null ||
			selectedTypes.size > 0 ||
			selectedRelations.size > 0 ||
			!!relation.trim() ||
			!!object.trim(),
	);

	/** Reset EVERYTHING — query, answer, facets, selection — back to the whole graph at rest. */
	function clearAll(): void {
		kind = 'why';
		user = me ? `user:${me.sub}` : '';
		relation = '';
		object = '';
		objectType = 'table';
		depth = 3;
		seed = '';
		selected = null;
		selectedTypes = new Set();
		selectedRelations = new Set();
		answer = null;
		verdict = null;
		chain = [];
		failure = null;
		lastPushed = '';
		void goto('?', { replaceState: true, keepFocus: true, noScroll: true });
	}

	function onNodeClick(event: { node: { id: string } }): void {
		selected = event.node.id;
		seed = event.node.id;
		void loadNeighbourhood(event.node.id);
		pushUrl({ seed: event.node.id });
	}
</script>

<!-- 10.5rem, measured, not 9: navbar + breadcrumb + page padding + the page's own title row sit above
     this component, and the old 9rem budget pushed the canvas ~1.5rem past the viewport — which CLIPPED
     Svelte Flow's Controls and the legend at the bottom and read as "the canvas is small and things
     overlap". The map takes all the room there actually is once nothing hangs off-screen. -->
<div class="flex h-[calc(100vh-10.5rem)] flex-col gap-3">
	<QueryBar
		bind:kind
		bind:user
		bind:relation
		bind:object
		bind:objectType
		bind:depth
		{busy}
		types={modelTypes}
		relations={relationOptions}
		subjects={['user:', 'team:', 'role:']}
		objects={objectOptions}
		onrun={() => {
	void run();
	pushUrl({ run: '1' });
	// Recorded from `lastPushed` — the string pushUrl just wrote — so the history replays the exact
	// query the address bar holds and cannot drift from it.
	if (lastPushed) history = recordQuery(lastPushed);
}}
	/>

	{#if hasQuery}
		<div class="flex items-center gap-2">
			<Button size="sm" variant="ghost" onclick={clearAll}>
				<RotateCcw size={12} /> Clear query
			</Button>
			<span class="text-xs text-muted-foreground">back to the whole graph</span>
		</div>
	{/if}

	<!-- Recent queries. The URL makes one question shareable; this makes the investigation retraceable.
	     Nothing is stored that the URL does not already carry — see history.ts — because subject and
	     object ids are exactly the sensitive part of this surface. -->
	{#if history.length}
		<details class="rounded-md border border-border px-2 py-1.5">
			<summary class="cursor-pointer text-xs font-medium">
				Recent queries <span class="text-muted-foreground">({history.length})</span>
			</summary>
			<ul class="mt-1.5 flex flex-col gap-0.5">
				{#each history as entry (entry.query)}
					<li>
						<button
							class="w-full truncate rounded px-1.5 py-1 text-left font-mono text-[11px] hover:bg-muted"
							title={entry.label}
							onclick={() => {
	// Replay the stored query VERBATIM through the same hydration path a pasted link takes — so a
	// re-run cannot diverge from what the entry says it was.
	lastPushed = null;
	void goto(`?${entry.query}`, { keepFocus: true, noScroll: true });
}}
						>
							{entry.label}
						</button>
					</li>
				{/each}
			</ul>
			<button
				class="mt-1 px-1.5 text-[11px] text-muted-foreground hover:text-foreground"
				onclick={() => (history = clearHistory())}
			>
				Clear history
			</button>
		</details>
	{/if}

	<!-- DATA vs MODEL. Two different questions about the same authorization system: what is granted
	     right now, and what the schema permits at all. A toggle rather than a tab — the canvas stays
	     mounted and the query survives the switch. -->
	<div class="flex items-center gap-1.5" role="group" aria-label="Graph source">
		<Button
			size="sm"
			variant={view === 'data' ? 'default' : 'outline'}
			aria-pressed={view === 'data'}
			onclick={() => {
	view = 'data';
	pushUrl({});
}}
		>
			Live data
		</Button>
		<Button
			size="sm"
			variant={view === 'model' ? 'default' : 'outline'}
			aria-pressed={view === 'model'}
			onclick={() => {
	view = 'model';
	pushUrl({});
}}
		>
			Model schema
		</Button>
		<span class="text-xs text-muted-foreground">
			{view === 'model'
				? 'the types and relations themselves — no tuples involved'
				: 'who holds what in the live store right now'}
		</span>
	</div>

	<!-- One-click entry points. The old tabbed view had registry chips and this one dropped them for a
	     free-text box, which meant the only way in was to already know that a subject id is
	     `user:CiQwOGE4Njg0Yi1kYjg4…` and an object is `table:bronze$pages`. Nobody knows that. -->
	<div class="flex flex-wrap items-center gap-1.5 text-xs">
		<span class="text-muted-foreground">Jump to:</span>
		{#if me}
			<Button
				size="sm"
				variant="outline"
				title="Your own OIDC subject — what the tuple store actually keys on"
				onclick={() => {
	user = `user:${me?.sub}`;
	kind = 'what';
	pushUrl({});
}}
			>
				me{me.name ? ` (${me.name})` : ''}
			</Button>
		{/if}
		{#each entryPoints as entry (entry)}
			<Button
				size="sm"
				variant="outline"
				class="font-mono"
				onclick={() => {
	object = entry;
	selected = entry;
	kind = 'who';
	// The visible half of the jump: the canvas glides to the node and the inspector fills. Without
	// this the click only prefilled form fields, which read as the chip doing nothing.
	focusTarget = { id: entry };
	pushUrl({ seed: entry });
}}
			>
				{entry}
			</Button>
		{:else}
			<span class="text-muted-foreground">
				nothing registered yet — the graph below is the whole tuple store
			</span>
		{/each}
	</div>

	{#if truncated}
		<p class="text-xs text-warning">
			The store is larger than this view loads — the graph is PARTIAL. Narrow it with the facets, or
			query for what you need.
		</p>
	{/if}

	{#if denied}
		<div class="flex items-center gap-2 rounded-md border border-border p-3 text-sm">
			<ShieldAlert size={16} /> The authorization store is estate-admin only.
		</div>
	{:else}
		{#if failure}
			<p class="text-xs text-destructive">{failure}</p>
		{/if}

		<!-- The filter is a POPOVER, not a resident column. The rail used to hold 208px of the viewport
		     permanently for a control that is idle most of the time — on this page the canvas is the
		     product, and every horizontal pixel spent on chrome is spent against it. The button carries
		     the two facts that must stay visible while it is closed: how many nodes are shown, and
		     whether a filter is active at all. -->
		<div class="flex items-center gap-2">
			<Popover.Root>
				<Popover.Trigger>
					{#snippet child({ props }: { props: Record<string, unknown> })}
						<Button size="sm" variant={activeFacets ? 'default' : 'outline'} {...props}>
							<Filter size={13} />
							Filter
							{#if activeFacets}
								<Badge variant="secondary" class="ml-1 px-1.5 font-mono text-[10px]">
									{filtered.nodes.length}/{composed.nodes.length}
								</Badge>
							{/if}
						</Button>
					{/snippet}
				</Popover.Trigger>
				<Popover.Content align="start" class="w-60 p-3">
					<FacetRail
						typeCounts={composed.typeCounts}
						relationCounts={composed.relationCounts}
						{selectedTypes}
						{selectedRelations}
						shown={filtered.nodes.length}
						total={composed.nodes.length}
						onchange={(next) => {
	selectedTypes = next.types;
	selectedRelations = next.relations;
	pushUrl({});
}}
					/>
				</Popover.Content>
			</Popover.Root>
			{#if activeFacets}
				<span class="text-xs text-muted-foreground">
					{filtered.nodes.length} of {composed.nodes.length} nodes shown
				</span>
			{/if}
		</div>

		<div class="flex min-h-0 flex-1 gap-4">
			<!-- Container-level reset: per-element pointerleave is lost on fast exits over SVG, and a
			     hover emphasis that survives its own pointer is a bug regardless of how subtle it is.
			     role="presentation": the handler is housekeeping on a layout wrapper, not interactivity —
			     nothing here is focusable or announced. -->
			<div
				role="presentation"
				class="relative min-w-0 flex-1 overflow-hidden rounded-md border border-border"
				onpointerleave={() => {
	hovered = null;
	hoveredEdge = null;
}}
			>
				{#if composed.nodes.length === 0}
					<div
						class="flex h-full items-center justify-center p-6 text-center text-sm text-muted-foreground"
					>
						Ask a question above to put the live graph on the canvas.
					</div>
				{:else}
					<GraphCanvas
						bind:nodes
						bind:edges
						{nodeTypes}
						focus={focusTarget}
						fitTrigger={`${view}:${filtered.nodes.length}:${filtered.edges.length}`}
						fitPadding={0.2}
						fitMaxZoom={1.25}
						onnodeclick={onNodeClick}
						onnodepointerenter={(e: { node: { id: string } }) => (hovered = e.node.id)}
						onnodepointerleave={() => (hovered = null)}
						onedgepointerenter={(e: { edge: { id: string } }) => (hoveredEdge = e.edge.id)}
						onedgepointerleave={() => (hoveredEdge = null)}
					/>
				{/if}
				{#if answer}
					<Badge variant="secondary" class="absolute right-2 top-2 font-mono text-[10px]">
						{chain.length} hop{chain.length === 1 ? '' : 's'} lit
					</Badge>
				{/if}
				<!-- The encodings, named ON the canvas. Dashed-means-expiring and dimmed-means-filtered are
				     inventions of this view; a reader should not need the source code to decode them. One
				     line, muted, bottom-RIGHT — bottom-left belongs to Svelte Flow's Controls, and the two
				     overlapped there. -->
				{#if composed.nodes.length > 0}
					<div
						class="pointer-events-none absolute bottom-2 right-2 rounded bg-background/80 px-2 py-0.5 text-[10px] text-muted-foreground"
						data-slot="canvas-legend"
					>
						<span class="text-primary">━</span> derivation path ·
						<span class="text-success">━</span> grant ·
						<span class="text-warning">━</span> role/team ·
						<span>─</span> parent ·
						<span>┅ ⏳</span> time-boxed ·
						<span class="opacity-50">▢</span> dimmed = filtered out or off-query
					</div>
				{/if}
			</div>

			<Inspector
				{selected}
				{verdict}
				{chain}
				{dsl}
				assignableRelations={assignableForSelected}
				mySub={me?.sub ?? null}
				{supportingTuples}
				{modelConditions}
				onchanged={() => {
	void loadNeighbourhood(seed);
}}
			/>
		</div>
	{/if}
</div>
