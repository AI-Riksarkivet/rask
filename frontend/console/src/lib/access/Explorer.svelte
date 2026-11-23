<script lang="ts">
	// The whole view. One canvas that stays mounted, a query bar that highlights it, a facet rail that
	// narrows it, and an inspector beside it — replacing four tabs that shared no state, no URL and no
	// question. Switching "tabs" used to throw away the graph you were looking at; nothing here does.
	import { parentNamespaceOrNull } from '#lib/api/identifiers.ts';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button, buttonVariants } from '@riksarkivet/ui/button';
	import * as Collapsible from '@riksarkivet/ui/collapsible';
	import * as Popover from '@riksarkivet/ui/popover';
	import { cn } from '@riksarkivet/ui/utils';
	import { afterNavigate, goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { Filter, RotateCcw, ShieldAlert } from '@lucide/svelte';
	import { namedObject, parseModelTypes } from '../access';
	import { accessMe, fetchAccessModel, fetchStore } from '../remote/access.api';
	import { fetchTables } from '#lib/data/remote/catalog.api.ts';
	import { controlTick, liveRead } from '#lib/live/tick.svelte.ts';
	import AccessCanvas from './AccessCanvas.svelte';
	import { askAccess, type Verdict } from './access-query';
	import FacetRail from './FacetRail.svelte';
	import Inspector from './Inspector.svelte';
	import QueryBar from './QueryBar.svelte';
	import {
		applyFacets,
		type BuiltGraph,
		buildWholeGraph,
		asQueryKind,
		idType,
		isSubject,
		overlay,
		type QueryKind,
	} from './graph';
	import { buildModelGraph, definedRelations } from './model-graph';
	import { clearHistory, type HistoryEntry, loadHistory, recordQuery } from './history';

	const EMPTY: BuiltGraph = {
		nodes: [],
		edges: [],
		typeCounts: new Map(),
		relationCounts: new Map(),
	};

	// ── URL is the state: a view must be linkable, and back/forward must work ──
	const params = $derived(page.url.searchParams);
	// Each field is the URL's until someone types into it: a writable derived, which a navigation (back,
	// forward, a pasted link, this view's own `pushUrl`) sets again from the address it lands on. Every
	// field is written to the URL by `pushUrl`, so this view's own push reads back what it wrote.
	let kind = $derived<QueryKind>(asQueryKind(params.get('q')) ?? 'why');
	let user = $derived(params.get('user') ?? '');
	let relation = $derived(params.get('relation') ?? '');
	let object = $derived(params.get('object') ?? '');
	let objectType = $derived(params.get('type') ?? 'table');
	let depth = $derived(Number(params.get('depth') ?? '3') || 3);
	let seed = $derived(params.get('seed') ?? '');
	let selectedTypes = $derived(new Set((params.get('types') ?? '').split(',').filter(Boolean)));
	let selectedRelations = $derived(new Set((params.get('rels') ?? '').split(',').filter(Boolean)));
	/** `data` = the live store (who has what NOW). `model` = the schema (what CAN grant what, ever).
	 *  A toggle beside the canvas, not a tab: the tabs are dead and stay dead, and switching must not
	 *  throw away the query you were reading. */
	let view = $derived<'data' | 'model'>(params.get('view') === 'model' ? 'model' : 'data');
	/** Recent queries. The URL makes ONE query shareable; this makes the SEQUENCE retraceable, which
	 *  replacing the history entry (done so the address bar does not fill with keystrokes) otherwise destroys. */
	let history = $state<HistoryEntry[]>([]);

	// The exact query string this component last wrote, so a navigation to it is known as this view's
	// own and does not run the query again, while a real history navigation (back/forward, or a pasted
	// link) does. Plain bookkeeping, not `$state`: nothing renders from it.
	let lastPushed: string | null = null;

	const activeFacets = $derived(selectedTypes.size > 0 || selectedRelations.size > 0);

	// A navigation that names a query to run (`run=1`, a pasted link or a replayed history entry) runs
	// it once the address has landed; the router says when, so this is its callback and not an effect.
	afterNavigate(() => {
		const q = page.url.searchParams;
		if (q.toString() === lastPushed) return;
		if (seed) loadNeighbourhood(seed);
		if (q.get('run') === '1') void run();
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
		// eslint-disable-next-line svelte/no-navigation-without-resolve -- a query on the current page, and resolve() refuses one that does not start with /
		void goto(`?${lastPushed}`, { replace: true, reset: false });
	}

	// ── data ──
	let answer = $state<BuiltGraph | null>(null);
	let verdict = $state<Verdict | null>(null);
	let chain = $state<string[]>([]);
	let busy = $state(false);
	/** A query's own refusal or failure, cleared by the next run. */
	let queryFailure = $state<string | null>(null);
	let queryDenied = $state(false);
	let selected = $state<string | null>(null);

	/** The model, read once: model changes are code migrations, not runtime events, so no cursor can
	 *  report one. */
	const modelRead = fetchAccessModel();
	const model = $derived(modelRead.current?.ok ? modelRead.current.data : null);
	const dsl = $derived(model?.dsl ?? null);
	const modelTypes = $derived(dsl ? parseModelTypes(dsl) : []);
	/** `{condition: {parameter: type}}` served by GET /management/v1/access/model — the grant dialog renders TYPED
	 *  inputs from this rather than from a copy that would drift the moment the model gains a parameter. */
	const modelConditions = $derived(model?.conditions ?? {});
	/** The estate's tables, from the one read every page shares: a table dropped or declared anywhere
	 *  refreshes it, so the seed list here is never a copy of its own. */
	const tablesRead = fetchTables();
	const registry = $derived.by(() => {
		const read = tablesRead.current;
		return read?.ok && Array.isArray(read.data.tables) ? [...read.data.tables].sort() : [];
	});
	/**
	 * The WHOLE tuple store is the base graph: show every relationship, then let a query highlight within
	 * it and the facets narrow it. `fetchStore` pages in the browser up to a bounded ceiling, and a store
	 * larger than it is reported as truncated rather than silently clipped, because a graph that quietly
	 * omits half the estate is worse than one that admits it.
	 */
	const storeRead = fetchStore();
	const storeResult = $derived(storeRead.current);
	/** Every tuple in the store — the canvas's base graph, and what the facet counts are computed on. */
	const storeTuples = $derived(storeResult?.ok ? storeResult.data.tuples : []);
	// `focus` on nothing: the whole store has no single centre, so every node is ordinary until a
	// query names one. buildWholeGraph lays subjects left and objects right off the tuple direction.
	const neighbourhood = $derived(storeResult?.ok ? buildWholeGraph(storeResult.data.tuples) : null);
	const storeDenied = $derived(
		storeResult?.ok === false && (storeResult.status === 401 || storeResult.status === 403),
	);
	const modelDenied = $derived(
		modelRead.current?.ok === false &&
			(modelRead.current.status === 401 || modelRead.current.status === 403),
	);
	const denied = $derived(queryDenied || storeDenied || modelDenied);
	const failure = $derived(
		queryFailure ?? (storeResult?.ok === false && !storeDenied ? storeResult.detail : null),
	);
	/** The names the last query's answer gave its table and namespace objects (`table:<uuid>` → `db$t`). */
	let answerNames = $state.raw<ReadonlyMap<string, string>>(new Map());
	/**
	 * The name each table and namespace object holds now. OpenFGA holds both by uuid, so a node, a chip or a hop
	 * would read `table:98792568-…` with nothing to say which table; the catalog answers each object's current name
	 * beside it, on the tuple reads and on every query, and the uuid stays the node's id.
	 */
	const storeNames = $derived(
		new Map(storeTuples.flatMap((t) => (t.name ? [[t.object, t.name] as const] : []))),
	);
	const objectNames = $derived(new Map([...answerNames, ...storeNames]));
	/** An id as a person reads it: a named object as `<type>:<name>`, anything else as written. */
	const shown = (id: string): string => namedObject(id, objectNames.get(id));
	/** The store is bigger than MAX_GRAPH_PAGES. Said out loud rather than silently clipped. */
	const truncated = $derived(storeResult?.ok ? storeResult.data.truncated : false);
	/**
	 * The signed-in identity. Load-bearing, not decoration: `sub` here is the principal key the tuple store
	 * keys on (`<idp-id>~<user id>`, a long id like `dex~08a8684b-…`, LH-063). Nobody types that, and the
	 * obvious `alice` names no principal. So the subject field is seeded from this, and a "me" chip puts it
	 * back one click away.
	 */
	const meRead = accessMe();
	// Fail-soft to null (no session, catalog down): the "me" chip simply does not render.
	const me = $derived(meRead.current ?? null);

	onMount(() => {
		history = loadHistory();
		// Seed the subject once the identity is read, only if the user has not already typed or linked one.
		void meRead.then((parsed) => {
			if (parsed && !user.trim()) user = parsed.sub;
		});
	});

	// The resting graph is LIVE: read once on mount (liveRead's unconditional first read), then again
	// whenever the CONTROL cursor advances — a grant landing anywhere on the estate (another admin, the
	// per-object grant API, a bootstrap job) redraws this canvas without a timer. The registry rides the
	// same tick because table/namespace mutations move the same cursor. The model does not: model
	// changes are code migrations, not runtime events, so no cursor can report one.
	liveRead(controlTick, () => {
		fetchStore();
		void tablesRead.refresh();
	});

	/** Relations the model defines on the type currently in play — the query bar's suggestions, so a
	 *  phantom relation is a keystroke away from impossible rather than a 400 one round trip later. */
	const relationOptions = $derived(
		dsl
			? definedRelations(dsl, kind === 'what' ? objectType : idType(object || seed || 'table'))
			: [],
	);

	/** The relations a grant on the SELECTED object may name — the model's directly-assignable ones for
	 *  that type, so the grant dialog offers a choice rather than a free-text box the catalog corrects. */
	const assignableForSelected = $derived(
		dsl && selected ? definedRelations(dsl, idType(selected), true) : [],
	);

	/** The handful of ids worth offering as one-click seeds: the estate root, every namespace, every
	 *  registered table. Derived from the LIVE store and registry, so it is never a hardcoded guess. */
	const entryPoints = $derived.by(() => {
		const fromStore = [...new Set(storeTuples.flatMap((t) => [t.user, t.object]))]
			.filter((id) => !isSubject(id))
			.sort((a, b) => shown(a).localeCompare(shown(b)));
		// A registered table the store already holds by its uuid is offered once, as that uuid object.
		const named = new Set(fromStore.map(shown));
		const fromRegistry = registry.map((t) => `table:${t}`).filter((id) => !named.has(id));
		return [...new Set([...fromStore, ...fromRegistry])].slice(0, 12);
	});

	const objectOptions = $derived([
		...registry.map((t) => `table:${t}`),
		// `parentNamespaceOrNull`, NOT a split on the first delimiter, which is what this line did. These become
		// FGA object ids the user then runs a check against, so for a nested table `acme$bronze$pages` the
		// old form offered `namespace:acme` — a different object from the real parent `acme$bronze`, and
		// possibly one that does not exist — while never offering the parent at all. In the one view whose
		// job is answering "who can do what on THIS object", that is the wrong object.
		// The OrNull variant for the same reason one rung down: a ROOT-level table has no parent
		// namespace at all (`parent_namespace_id` returns None), so offering `namespace:<its own name>`
		// would invent a second non-existent object right where the first one was removed.
		...[...new Set(registry.map(parentNamespaceOrNull).filter((ns) => ns !== null))].map(
			(ns) => `namespace:${ns}`,
		),
	]);

	/** One object's own tuples, for the inspector — the canvas already holds the whole store. */
	function loadNeighbourhood(target: string): void {
		if (target) fetchStore();
	}

	function fail(res: { status: number; detail: string }): void {
		queryDenied = res.status === 401 || res.status === 403;
		queryFailure = queryDenied ? 'This surface is estate-admin only.' : res.detail;
	}

	async function run(): Promise<void> {
		if (busy) return;
		busy = true;
		queryFailure = null;
		verdict = null;
		chain = [];
		answer = null;
		try {
			const out = await askAccess(kind, { user, relation, object, objectType, depth }, storeNames);
			if (!out.ok) {
				fail(out);
				return;
			}
			answerNames = out.names;
			answer = out.answer;
			verdict = out.verdict;
			chain = out.chain;
			selected = out.selected;
			if (out.seed !== null) seed = out.seed;
			if (out.note !== null) queryFailure = out.note;
		} finally {
			busy = false;
		}
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

	/** The node the last "Jump to" chip named — the canvas glides its viewport to it, so the chip has a
	 *  visible consequence beyond prefilled form fields. A fresh object per click, so clicking the same
	 *  chip again re-centres (see FlowFocus). */
	let focusTarget = $state<{ id: string } | null>(null);

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

	/** Replay a stored query VERBATIM through the same hydration path a pasted link takes, so a re-run
	 *  cannot diverge from what the history entry says it was. */
	function replay(query: string): void {
		lastPushed = null;
		// eslint-disable-next-line svelte/no-navigation-without-resolve -- a query on the current page, and resolve() refuses one that does not start with /
		void goto(`?${query}`, { reset: false });
	}

	/** Reset EVERYTHING — query, answer, facets, selection — back to the whole graph at rest. */
	function clearAll(): void {
		kind = 'why';
		user = me?.sub ?? '';
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
		queryFailure = null;
		lastPushed = '';
		// The fields follow the emptied URL; the subject goes back to the signed-in principal after it.
		// eslint-disable-next-line svelte/no-navigation-without-resolve -- a query on the current page, and resolve() refuses one that does not start with /
		void goto('?', { replace: true, reset: false }).then(() => (user = me?.sub ?? ''));
	}

	function onNodeClick(event: { node: { id: string } }): void {
		selected = event.node.id;
		seed = event.node.id;
		loadNeighbourhood(event.node.id);
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
		subjects={['user:', 'role:']}
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
		<Collapsible.Root class="rounded-md border border-border px-2 py-1.5">
			<Collapsible.Trigger
				class={cn(buttonVariants({ variant: 'ghost', size: 'xs' }), 'w-full justify-start')}
			>
				Recent queries <span class="text-muted-foreground">({history.length})</span>
			</Collapsible.Trigger>
			<Collapsible.Content>
				<ul class="mt-1.5 flex flex-col gap-0.5">
					{#each history as entry (entry.query)}
						<li>
							<Button
								variant="ghost"
								size="xs"
								class="w-full justify-start font-mono"
								title={entry.label}
								onclick={() => replay(entry.query)}
							>
								<span class="truncate">{entry.label}</span>
							</Button>
						</li>
					{/each}
				</ul>
				<Button
					variant="ghost"
					size="xs"
					class="text-muted-foreground mt-1"
					onclick={() => (history = clearHistory())}
				>
					Clear history
				</Button>
			</Collapsible.Content>
		</Collapsible.Root>
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

	<!-- One-click entry points, so the way in does not depend on already knowing that a person is
	     `dex~08a8684b-…` and an object is `table:bronze$pages`. -->
	<div class="flex flex-wrap items-center gap-1.5 text-xs">
		<span class="text-muted-foreground">Jump to:</span>
		{#if me}
			<Button
				size="sm"
				variant="outline"
				title="Your own principal — what the tuple store keys on"
				onclick={() => {
					user = me?.sub ?? '';
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
				title={entry}
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
				{shown(entry)}
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
			<AccessCanvas
				graph={filtered}
				total={composed.nodes.length}
				names={objectNames}
				focus={focusTarget}
				fitTrigger={`${view}:${filtered.nodes.length}:${filtered.edges.length}`}
				litHops={answer ? chain.length : null}
				onnodeclick={onNodeClick}
			/>

			<Inspector
				{selected}
				selectedName={selected ? (objectNames.get(selected) ?? null) : null}
				{verdict}
				{chain}
				{dsl}
				assignableRelations={assignableForSelected}
				mySub={me?.sub ?? null}
				{supportingTuples}
				{modelConditions}
				onchanged={() => {
					loadNeighbourhood(seed);
				}}
			/>
		</div>
	{/if}
</div>
