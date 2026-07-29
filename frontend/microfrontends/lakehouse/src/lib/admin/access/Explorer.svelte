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
	import {
		Background,
		BackgroundVariant,
		Controls,
		type Edge,
		MarkerType,
		SvelteFlow,
	} from '@xyflow/svelte';
	import { Badge } from '@rask/ui/badge';
	import { goto } from '$app/navigation';
	import { onMount, untrack } from 'svelte';
	import { page } from '$app/state';
	import { parse } from '@rask/api';
	import { ShieldAlert } from '@lucide/svelte';
	import {
		AccessModelSchema,
		checkAccess,
		CheckVerdictSchema,
		expand,
		ExpandResultSchema,
		fetchAccessModel,
		fetchTables,
		fetchTuples,
		listObjects,
		ListObjectsResultSchema,
		listUsers,
		ListUsersResultSchema,
		parseModelTypes,
		type Tuple,
		TuplesPageSchema,
	} from '../access';
	import FacetRail from './FacetRail.svelte';
	import Inspector from './Inspector.svelte';
	import QueryBar from './QueryBar.svelte';
	import {
		applyFacets,
		type BuiltGraph,
		buildNeighbourhood,
		idType,
		layout,
		overlay,
		type QueryKind,
		walkDerivation,
	} from './graph';

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
	let registry = $state<string[]>([]);

	// onMount, not $effect: these two fetch once and depend on nothing reactive. An $effect with no
	// dependencies happens to run once too, which is exactly why it is the wrong tool — it states
	// "re-run when something changes" about code that has nothing to re-run for.
	onMount(() => {
		void loadModel();
		void loadRegistry();
	});

	async function loadModel(): Promise<void> {
		const res = await fetchAccessModel();
		if (!res.ok) {
			denied = res.status === 401 || res.status === 403;
			return;
		}
		try {
			const parsed = parse(AccessModelSchema, res.data);
			dsl = parsed.dsl;
			modelTypes = parseModelTypes(parsed.dsl);
		} catch (err) {
			console.error(`access model parse failure: ${String(err)}`);
		}
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

	const objectOptions = $derived([
		...registry.map((t) => `table:${t}`),
		...[...new Set(registry.map((t) => (t.includes('$') ? t.slice(0, t.indexOf('$')) : t)))].map(
			(ns) => `namespace:${ns}`,
		),
	]);

	async function loadNeighbourhood(target: string): Promise<void> {
		if (!target) return;
		const res = await fetchTuples({ object: target, pageSize: 100 });
		if (!res.ok) {
			denied = res.status === 401 || res.status === 403;
			return;
		}
		try {
			const tuples: Tuple[] = parse(TuplesPageSchema, res.data).tuples;
			neighbourhood = buildNeighbourhood(target, tuples);
		} catch (err) {
			console.error(`neighbourhood parse failure: ${String(err)}`);
		}
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
		const parsed = parse(ListObjectsResultSchema, res.data);
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
		const parsed = parse(ListUsersResultSchema, res.data);
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
		const probed = parse(CheckVerdictSchema, checkRes.data);
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
		const parsed = parse(ExpandResultSchema, expandRes.data);
		answer = walkDerivation(parsed.tree, parsed.object, parsed.relation);
		seed = parsed.object;
		chain = answer.edges
			.filter((e) => e.onPath)
			.map((e) => `${e.source} → ${e.target} (${e.label})`);
	}

	// ── the rendered graph ──
	const composed = $derived(overlay(neighbourhood ?? EMPTY, answer));
	const filtered = $derived(
		applyFacets(composed, { types: selectedTypes, relations: selectedRelations }),
	);

	let nodes = $state.raw<AccessNodeType[]>([]);
	let edges = $state.raw<Edge[]>([]);

	$effect(() => {
		const positioned = layout(filtered.nodes);
		nodes = positioned.map((n) => ({
			id: n.id,
			type: 'access' as const,
			position: { x: n.x, y: n.y },
			data: { fgaId: n.id, fgaType: n.fgaType, label: n.label, role: n.role, via: n.via },
		}));
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
			label: e.label,
			animated: e.onPath,
			markerEnd: {
				type: MarkerType.ArrowClosed,
				width: 18,
				height: 18,
				color: e.onPath ? 'var(--primary)' : 'var(--muted-foreground)',
			},
			style: e.onPath
				? 'stroke: var(--primary); stroke-width: 2;'
				: 'stroke: var(--muted-foreground); opacity: 0.35;',
		}));
	});

	function onNodeClick(event: { node: { id: string } }): void {
		selected = event.node.id;
		seed = event.node.id;
		void loadNeighbourhood(event.node.id);
		pushUrl({ seed: event.node.id });
	}
</script>

<div class="flex h-[calc(100vh-9rem)] flex-col gap-3">
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
}}
	/>

	{#if denied}
		<div class="flex items-center gap-2 rounded-md border border-border p-3 text-sm">
			<ShieldAlert size={16} /> The authorization store is estate-admin only.
		</div>
	{:else}
		{#if failure}
			<p class="text-xs text-destructive">{failure}</p>
		{/if}

		<div class="flex min-h-0 flex-1 gap-4">
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

			<div class="relative min-w-0 flex-1 overflow-hidden rounded-md border border-border">
				{#if composed.nodes.length === 0}
					<div
						class="flex h-full items-center justify-center p-6 text-center text-sm text-muted-foreground"
					>
						Ask a question above to put the live graph on the canvas.
					</div>
				{:else}
					<SvelteFlow bind:nodes bind:edges {nodeTypes} fitView onnodeclick={onNodeClick}>
						<Background variant={BackgroundVariant.Dots} gap={16} />
						<Controls />
					</SvelteFlow>
				{/if}
				{#if answer}
					<Badge variant="secondary" class="absolute right-2 top-2 font-mono text-[10px]">
						{chain.length} hop{chain.length === 1 ? '' : 's'} lit
					</Badge>
				{/if}
			</div>

			<Inspector
				{selected}
				{verdict}
				{chain}
				{dsl}
				onchanged={() => {
	void loadNeighbourhood(seed);
}}
			/>
		</div>
	{/if}
</div>
