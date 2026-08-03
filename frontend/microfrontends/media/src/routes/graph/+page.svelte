<script lang="ts">
	/**
	 * /graph — knowledge-graph explorer (Kuzu-Explorer-style shell over lance-graph).
	 *
	 * A query bar (entity search + canned Cypher presets + a REPL textarea) drives
	 * a result area with three views: Graph (a d3-force layout rendered by the
	 * WebGPU <GpuGraph>), Table, and JSON. Picking a node loads its side panel —
	 * properties, clips (each deep-links into the player at /?doc=&t=), and
	 * clickable relationship / co-occurrence chips that re-centre the subgraph.
	 */

	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import {
		forceSimulation,
		forceManyBody,
		forceLink,
		forceCollide,
		forceX,
		forceY,
		type Simulation,
		type SimulationNodeDatum,
	} from 'd3-force';
	import { Button } from '@rask/ui';
	import { Select } from '@rask/ui/select';
	import { Input } from '@rask/ui/input';
	import { type SelectOption } from '@rask/ui/select';
	import { HelpCircle } from '@lucide/svelte';
	import GpuGraph from '$lib/graph/gpu-graph.svelte';
	import GraphBreadcrumb from '$lib/graph/graph-breadcrumb.svelte';
	import {
		activeView,
		getGraphStatus,
		getGraphSubgraph,
		getGraphEntity,
		searchGraphEntities,
		type GraphStatus,
		type GraphNode,
		type GraphEntityResponse,
		type GraphMatch,
		type GraphCypherResponse,
	} from '@rask/media-api';
	import { runGraphCypher } from '$lib/graph/remote/graph.remote';

	type View = 'graph' | 'table' | 'json';
	type SimNode = SimulationNodeDatum & { id: string; idx: number };

	// ── type → colour (Tailwind 400; >1 video = a lighter, "brighter" variant) ──
	const TYPE_RGB: Record<string, [number, number, number]> = {
		PERSON: [56, 189, 248], // sky-400
		ORG: [52, 211, 153], // emerald-400
		GEO: [251, 191, 36], // amber-400
		EVENT: [167, 139, 250], // violet-400
		CONCEPT: [244, 114, 182], // pink-400 — ideas / policy areas / methods
		WORK: [251, 146, 60], // orange-400 — reports / artifacts / datasets
		OTHER: [148, 163, 184], // slate-400
	};
	function nodeColor(type: string, videos: number): [number, number, number] {
		const base = TYPE_RGB[type] ?? TYPE_RGB.OTHER!;
		if (videos <= 1) return base;
		return [
			Math.round(base[0] + (255 - base[0]) * 0.35),
			Math.round(base[1] + (255 - base[1]) * 0.35),
			Math.round(base[2] + (255 - base[2]) * 0.35),
		];
	}
	const sizeOf = (mentions: number): number =>
		8 + Math.min(20, Math.sqrt(Math.max(1, mentions)) * 4);

	const PRESETS: SelectOption[] = [
		{ value: '', label: 'Cypher presets…' },
		{
			value: 'MATCH (a:Entity)-[:MENTIONS]->(c:Chunk)\nRETURN a.name, c.doc_id, c.start_s LIMIT 25',
			label: 'Entity → its clips',
		},
		{
			value:
				'MATCH (a:Entity)-[:MENTIONS]->(c:Chunk)<-[:MENTIONS]-(b:Entity)\nWHERE a.entity_id < b.entity_id\nRETURN a.name, b.name, count(c.chunk_id) AS shared ORDER BY shared DESC LIMIT 15',
			label: 'Co-occurrence (shared clips)',
		},
		{
			value: 'MATCH (a:Entity)-[:RELATIONSHIP]->(b:Entity)\nRETURN a.name, b.name LIMIT 25',
			label: 'Relationship neighbourhood',
		},
		{
			value:
				'MATCH (a:Entity)\nRETURN a.name, a.entity_type, a.mention_count ORDER BY a.mention_count DESC LIMIT 20',
			label: 'Top entities by mentions',
		},
		{
			value:
				'MATCH (a:Entity)\nRETURN a.entity_type, count(a.entity_id) AS n ORDER BY n DESC LIMIT 10',
			label: 'Entity-type breakdown',
		},
	];

	// Plain-language question + what it shows + the Cypher. One click fills the
	// REPL and runs (all verified against the live engine).
	const HELP_EXAMPLES = [
		{
			label: 'Who are the people?',
			desc: 'Named individuals only (full names), ranked by how many clips mention them.',
			query:
				"MATCH (a:Entity) WHERE a.entity_type = 'PERSON' AND a.name CONTAINS ' ' RETURN a.name, a.mention_count ORDER BY a.mention_count DESC LIMIT 20",
		},
		{
			label: 'What were the topics?',
			desc: 'The most-discussed concepts and policy areas across every press conference.',
			query:
				"MATCH (a:Entity) WHERE a.entity_type = 'CONCEPT' RETURN a.name, a.mention_count ORDER BY a.mention_count DESC LIMIT 20",
		},
		{
			label: 'Central players (hubs)',
			desc: 'Entities with the most connections — the gravitational centres of the whole conversation.',
			query:
				'MATCH (a:Entity)-[:RELATIONSHIP]->(b:Entity) RETURN a.name, a.entity_type, count(b.entity_id) AS connections ORDER BY connections DESC LIMIT 15',
		},
		{
			label: 'Strongest links',
			desc: 'Relationships asserted most often (weight = shared clips), e.g. Sweden↔EU. Each row carries the relation text.',
			query:
				'MATCH (a:Entity)-[r:RELATIONSHIP]->(b:Entity) RETURN a.name, b.name, r.weight, r.description ORDER BY r.weight DESC LIMIT 15',
		},
		{
			label: "One person's network",
			desc: 'Everything Göran Persson is linked to. Swap the name (lower-case) to explore anyone.',
			query:
				"MATCH (a:Entity)-[:RELATIONSHIP]-(b:Entity) WHERE a.name_lower = 'göran persson' RETURN b.name, b.entity_type LIMIT 25",
		},
		{
			label: 'Who comes up with the EU?',
			desc: 'Organisations most often mentioned in the same clips as the EU (co-occurrence).',
			query:
				"MATCH (a:Entity)-[:MENTIONS]->(c:Chunk)<-[:MENTIONS]-(b:Entity) WHERE a.name_lower = 'eu' AND b.entity_type = 'ORG' AND b.name_lower <> 'eu' RETURN b.name, count(c.chunk_id) AS shared ORDER BY shared DESC LIMIT 12",
		},
		{
			label: 'A person on the timeline',
			desc: 'Every clip mentioning Carl Bildt, in order — click a result row to jump to that moment in the video.',
			query:
				"MATCH (a:Entity)-[:MENTIONS]->(c:Chunk) WHERE a.name_lower = 'carl bildt' RETURN c.doc_id, c.start_s, c.text ORDER BY c.start_s LIMIT 20",
		},
		{
			label: 'Topics tied to a place',
			desc: 'Which concepts come up alongside Sweden — what the country is discussed in terms of.',
			query:
				"MATCH (a:Entity)-[:MENTIONS]->(c:Chunk)<-[:MENTIONS]-(b:Entity) WHERE a.name_lower = 'sverige' AND b.entity_type = 'CONCEPT' RETURN b.name, count(c.chunk_id) AS shared ORDER BY shared DESC LIMIT 15",
		},
		{
			label: 'Search by word',
			desc: 'Find any entity whose name contains a word (try miljö, skatt, vård…).',
			query:
				"MATCH (a:Entity) WHERE a.name_lower CONTAINS 'miljö' RETURN a.name, a.entity_type, a.mention_count LIMIT 20",
		},
	];

	function runExample(query: string): void {
		cypherText = query;
		cypherOpen = true;
		void runCypher();
	}

	// ── page state ──────────────────────────────────────────────────────────
	let status = $state<GraphStatus | null>(null);
	let view = $state<View>('graph');
	let loading = $state(false);
	let showHelp = $state(false);

	let searchQ = $state('');
	let matches = $state<GraphMatch[]>([]);
	let dropdownOpen = $state(false);

	let presetValue = $state('');
	let cypherText = $state('');
	let cypherOpen = $state(false);
	let cypherResult = $state<GraphCypherResponse | null>(null);

	let detail = $state<GraphEntityResponse | null>(null);
	let selectedId = $state<string | null>(null);
	// navigation trail for the breadcrumb; empty = overview
	let trail = $state<{ id: string; name: string }[]>([]);

	// ── force-layout buffers (mutated in place; layoutVersion is the GPU signal)
	let graphNodes = $state.raw<GraphNode[]>([]);
	let idToIdx = $state.raw<Map<string, number>>(new Map());
	let nodesX = $state.raw(new Float32Array(0));
	let nodesY = $state.raw(new Float32Array(0));
	let nodeRgb = $state.raw(new Uint8Array(0));
	let nodeSize = $state.raw(new Float32Array(0));
	let nodeState = $state.raw(new Uint8Array(0));
	let edgesIdx = $state.raw(new Uint32Array(0));
	let layoutVersion = $state(0);
	let fitBounds = $state.raw({ minX: -1, minY: -1, maxX: 1, maxY: 1 });

	let sim: Simulation<SimNode, undefined> | null = null;
	let simNodes: SimNode[] = [];

	let gw = $state(0);
	let gh = $state(0);
	let hover = $state<{ name: string; type: string; x: number; y: number } | null>(null);

	const markerIndex = $derived(selectedId !== null ? (idToIdx.get(selectedId) ?? null) : null);

	function stopSim(): void {
		sim?.stop();
		sim = null;
	}

	function recomputeFit(): void {
		const n = nodesX.length;
		if (n === 0) return;
		let minX = Infinity;
		let minY = Infinity;
		let maxX = -Infinity;
		let maxY = -Infinity;
		for (let i = 0; i < n; i++) {
			minX = Math.min(minX, nodesX[i]!);
			minY = Math.min(minY, nodesY[i]!);
			maxX = Math.max(maxX, nodesX[i]!);
			maxY = Math.max(maxY, nodesY[i]!);
		}
		const padX = (maxX - minX) * 0.08 || 1;
		const padY = (maxY - minY) * 0.08 || 1;
		fitBounds = { minX: minX - padX, minY: minY - padY, maxX: maxX + padX, maxY: maxY + padY };
	}

	function buildLayout(nodes: GraphNode[], edges: { source: string; target: string }[]): void {
		stopSim();
		const n = nodes.length;
		const idx = new Map(nodes.map((nd, i) => [nd.id, i]));
		const x = new Float32Array(n);
		const y = new Float32Array(n);
		const rgb = new Uint8Array(n * 3);
		const size = new Float32Array(n);
		const R = 40 + n * 1.5;
		for (let i = 0; i < n; i++) {
			const a = (i / Math.max(1, n)) * Math.PI * 2;
			x[i] = Math.cos(a) * R;
			y[i] = Math.sin(a) * R;
			const [r, g, b] = nodeColor(nodes[i]!.type, nodes[i]!.videos);
			rgb[i * 3] = r;
			rgb[i * 3 + 1] = g;
			rgb[i * 3 + 2] = b;
			size[i] = sizeOf(nodes[i]!.mentions);
		}
		const valid = edges.filter((e) => idx.has(e.source) && idx.has(e.target));
		const ei = new Uint32Array(valid.length * 2);
		valid.forEach((e, i) => {
			ei[i * 2] = idx.get(e.source)!;
			ei[i * 2 + 1] = idx.get(e.target)!;
		});

		graphNodes = nodes;
		idToIdx = idx;
		nodesX = x;
		nodesY = y;
		nodeRgb = rgb;
		nodeSize = size;
		nodeState = new Uint8Array(n);
		edgesIdx = ei;

		// Settle the force layout SYNCHRONOUSLY (milliseconds at these node
		// counts) so nodes mount already in their final positions and the camera
		// fits exactly once. A live-ticking sim fit the camera to the seed
		// circle, animated the collapse, then snap-refit on 'end' — three camera
		// moves per click that read as wild zooming and stomped any manual
		// pan/zoom made mid-transition.
		simNodes = nodes.map((nd, i) => ({ id: nd.id, idx: i, x: x[i], y: y[i] }));
		const links = valid.map((e) => ({ source: e.source, target: e.target }));
		sim = forceSimulation<SimNode>(simNodes)
			.stop()
			.force('charge', forceManyBody<SimNode>().strength(-220))
			.force(
				'link',
				forceLink<SimNode, { source: string; target: string }>(links)
					.id((d) => d.id)
					.distance(46)
					.strength(0.35),
			)
			.force(
				'collide',
				forceCollide<SimNode>((d) => sizeOf(graphNodes[d.idx]?.mentions ?? 1) / 2 + 5),
			)
			.force('x', forceX<SimNode>(0).strength(0.04))
			.force('y', forceY<SimNode>(0).strength(0.04));
		for (let tick = 0; sim.alpha() > 0.025 && tick < 400; tick++) sim.tick();
		for (let i = 0; i < simNodes.length; i++) {
			nodesX[i] = simNodes[i]!.x ?? 0;
			nodesY[i] = simNodes[i]!.y ?? 0;
		}
		recomputeFit();
		layoutVersion++;
	}

	// ── data loaders ──────────────────────────────────────────────────────────
	async function loadOverview(): Promise<void> {
		loading = true;
		try {
			const sub = await getGraphSubgraph(undefined, 120);
			selectedId = null;
			detail = null;
			trail = [];
			buildLayout(sub.nodes, sub.edges);
		} finally {
			loading = false;
		}
	}

	async function selectEntity(id: string): Promise<void> {
		dropdownOpen = false;
		view = 'graph';
		loading = true;
		try {
			const [sub, d] = await Promise.all([getGraphSubgraph(id, 60), getGraphEntity(id)]);
			selectedId = id;
			detail = d;
			// breadcrumb trail: revisiting a crumb truncates back to it, new hops append
			const at = trail.findIndex((c) => c.id === id);
			const name = d.entity?.name ?? id;
			trail = at >= 0 ? trail.slice(0, at + 1) : [...trail, { id, name }];
			buildLayout(sub.nodes, sub.edges);
		} finally {
			loading = false;
		}
	}

	async function runCypher(): Promise<void> {
		const q = cypherText.trim();
		if (!q) return;
		cypherOpen = true;
		view = 'table';
		// The dataset selector is a BROWSER-side store, so it travels as an argument now — the deleted
		// route read it off the query string the client had already appended.
		const res = await runGraphCypher({ query: q, dataset: activeView().datasetParam() });
		// A refused or unreachable console call lands in the response's own `error` field, which the
		// table pane already renders. Before this, a non-2xx threw an ApiError out of an unawaited
		// handler and the console simply showed the PREVIOUS result.
		cypherResult = res.ok ? res.data : { built: false, columns: [], rows: [], error: res.detail };
	}

	// The Select only exposes a bound value — react to a chosen preset here, then
	// clear it (guarded so the clear-write doesn't re-trigger) for re-selectability.
	$effect(() => {
		const v = presetValue;
		if (!v) return;
		cypherText = v;
		cypherOpen = true;
		presetValue = '';
		void runCypher();
	});

	let searchTimer: ReturnType<typeof setTimeout> | undefined;
	$effect(() => {
		const q = searchQ.trim();
		clearTimeout(searchTimer);
		if (q.length < 2) {
			matches = [];
			return;
		}
		searchTimer = setTimeout(() => {
			void searchGraphEntities(q).then((r) => {
				matches = r.matches;
				dropdownOpen = r.matches.length > 0;
			});
		}, 250);
	});

	function onClip(docId: string, start: number): void {
		void goto(`${base}/?doc=${encodeURIComponent(docId)}&t=${Math.max(0, Math.floor(start))}`);
	}

	function onPick(index: number | null): void {
		if (index === null) return;
		const node = graphNodes[index];
		if (node) void selectEntity(node.id);
	}

	function onHover(index: number | null, clientX: number, clientY: number): void {
		if (index === null) {
			hover = null;
			return;
		}
		const node = graphNodes[index];
		hover = node ? { name: node.name, type: node.type, x: clientX, y: clientY } : null;
	}

	const fmtTime = (s: number): string => {
		const m = Math.floor(s / 60);
		return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
	};

	onMount(() => {
		void getGraphStatus().then((s) => {
			status = s;
			if (s.built) void loadOverview();
		});
		return stopSim;
	});
</script>

<div class="flex h-full min-h-0">
	<!-- ── main column: query bar + result views ───────────────────────────── -->
	<div class="flex min-w-0 flex-1 flex-col">
		<!-- query bar -->
		<div class="border-border bg-card/40 flex flex-col gap-2 border-b p-3">
			<div class="flex flex-wrap items-center gap-2">
				<div class="relative w-64">
					<Input
						class="h-8 rounded-lg"
						type="search"
						placeholder="Search entities…"
						aria-label="Search entities"
						bind:value={searchQ}
						onfocus={() => (dropdownOpen = matches.length > 0)}
						onblur={() => setTimeout(() => (dropdownOpen = false), 150)}
					/>
					{#if dropdownOpen}
						<ul
							class="border-border bg-popover text-popover-foreground absolute z-20 mt-1 max-h-72 w-80 overflow-auto rounded-lg border p-1 shadow-md"
						>
							{#each matches as m (m.entity_id)}
								<li>
									<button
										class="hover:bg-muted flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm"
										onclick={() => selectEntity(m.entity_id)}
									>
										<span class="truncate">{m.name}</span>
										<span class="text-muted-foreground shrink-0 text-xs"
											>{m.entity_type} · {m.mention_count}×</span
										>
									</button>
								</li>
							{/each}
						</ul>
					{/if}
				</div>

				<Select
					options={PRESETS}
					bind:value={presetValue}
					placeholder="Cypher presets…"
					ariaLabel="Cypher presets"
				/>

				<Button variant="outline" size="sm" onclick={() => (cypherOpen = !cypherOpen)}>
					{cypherOpen ? 'Hide' : 'Cypher'}
				</Button>
				<Button variant="outline" size="sm" onclick={loadOverview}>Overview</Button>

				<div class="ml-auto flex items-center gap-2">
					<Button
						variant={showHelp ? 'secondary' : 'ghost'}
						size="icon-sm"
						aria-pressed={showHelp}
						onclick={() => (showHelp = !showHelp)}
						title="How to read this"
						aria-label="How to read this"
					>
						<HelpCircle />
					</Button>
					<!-- Result-view picker as one segmented control, matching the search page. -->
					<div
						class="border-border bg-background flex items-center gap-0.5 rounded-lg border p-0.5"
					>
						{#each ['graph', 'table', 'json'] as const as v (v)}
							<Button
								variant={view === v ? 'secondary' : 'ghost'}
								size="xs"
								aria-pressed={view === v}
								onclick={() => (view = v)}
								class="capitalize">{v}</Button
							>
						{/each}
					</div>
				</div>
			</div>

			<GraphBreadcrumb {trail} onNavigate={(id) => (id ? selectEntity(id) : loadOverview())} />

			{#if cypherOpen}
				<div class="flex items-start gap-2">
					<textarea
						class="border-input dark:bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 h-20 flex-1 rounded-lg border bg-transparent p-2 font-mono text-xs transition-colors outline-none focus-visible:ring-3"
						placeholder="MATCH (a:Entity)-[:RELATIONSHIP]->(b:Entity) RETURN a.name, b.name LIMIT 25"
						bind:value={cypherText}></textarea>
					<Button size="sm" onclick={runCypher}>Run</Button>
				</div>
			{/if}

			{#if showHelp}
				<div
					class="bg-muted/40 border-border text-muted-foreground rounded-lg border p-3 text-xs leading-relaxed"
				>
					<p class="text-foreground mb-1 font-medium">What is this?</p>
					A knowledge graph LightRAG extracted from the press-conference transcripts. Each
					<b>node is an entity</b> (person, organisation, place, event…) and each
					<b>edge is a relationship</b>
					it pulled from the text. Everything is queried live with Cypher via lance-graph.
					<p class="text-foreground mt-2 mb-1 font-medium">How to use it</p>
					<ul class="ml-4 list-disc space-y-0.5">
						<li><b>Search</b> an entity (top-left) or click a node → its side panel opens.</li>
						<li>
							The side panel lists <b>clips</b> (the 30 s moments it's mentioned — click one to play it),
							plus <b>related</b> and <b>co-occurring</b> entities (click to re-centre).
						</li>
						<li>
							<b>Cypher presets</b> / the REPL answer precise questions; results show in the
							<b>Table</b> / <b>JSON</b> tabs.
						</li>
					</ul>
					<p class="text-foreground mt-2 mb-1 font-medium">How to read the graph</p>
					<ul class="ml-4 list-disc space-y-0.5">
						<li>
							<b>Node size</b> = how many clips mention it; <b>brighter</b> = appears in &gt;1 video.
						</li>
						<li>
							<b>Colour</b> = type (legend, bottom-left). Drag to pan, scroll to zoom, “Fit” to recentre.
						</li>
						<li>
							The picture is for <i>navigating</i> — the precise answers live in the clips list and the Cypher/Table
							view.
						</li>
						<li>
							<b>One name can be several people.</b> A bare first name like <i>“Anders”</i> is one node,
							but the transcripts may mean different individuals — the AI links them by name only.
							<b>The clips disambiguate</b>: open the node and read each clip's context.
							<i>Full names</i> (“Göran Persson”) are unambiguous.
						</li>
					</ul>
					<p class="mt-1.5">
						Click an <b>example query</b> in the right-hand rail to run it. Full schema:
						<code>docs/GRAPH.md</code>.
					</p>
				</div>
			{/if}
		</div>

		<!-- result area + example-query rail -->
		<div class="flex min-h-0 flex-1">
			<div class="relative min-h-0 flex-1">
				{#if status && !status.built}
					<div class="grid h-full place-items-center p-6 text-center">
						<div class="max-w-sm">
							<p class="text-foreground text-sm font-medium">Knowledge graph not built yet</p>
							<p class="text-muted-foreground mt-1 text-xs">
								Run the LightRAG build + adapter to populate the <code>kg_*</code> tables, then reload.
							</p>
						</div>
					</div>
				{:else if view === 'graph'}
					<div class="absolute inset-0" bind:clientWidth={gw} bind:clientHeight={gh}>
						{#if gw > 0 && gh > 0 && graphNodes.length > 0}
							<GpuGraph
								{nodesX}
								{nodesY}
								{layoutVersion}
								{nodeRgb}
								{nodeSize}
								{nodeState}
								edges={edgesIdx}
								{fitBounds}
								width={gw}
								height={gh}
								background="#0a0a0a"
								{markerIndex}
								{onHover}
								{onPick}
							/>
						{:else}
							<div class="text-muted-foreground grid h-full place-items-center text-sm">
								{loading ? 'Loading graph…' : 'No nodes.'}
							</div>
						{/if}
						{#if hover}
							<div
								class="border-border bg-popover text-popover-foreground pointer-events-none fixed z-30 rounded-lg border px-2 py-1 text-xs shadow-md"
								style:left="{hover.x + 12}px"
								style:top="{hover.y + 12}px"
							>
								<span class="font-medium">{hover.name}</span>
								<span class="text-muted-foreground"> · {hover.type}</span>
							</div>
						{/if}
						<div class="absolute right-2 bottom-2">
							<Button variant="outline" size="sm" onclick={recomputeFit}>Fit</Button>
						</div>
						<div
							class="bg-card/70 border-border text-muted-foreground absolute bottom-2 left-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border px-2 py-1 text-[0.7rem] backdrop-blur"
						>
							{#each Object.entries(TYPE_RGB) as [t, rgb] (t)}
								<span class="flex items-center gap-1">
									<span
										class="size-2 rounded-full"
										style:background="rgb({rgb[0]},{rgb[1]},{rgb[2]})"
									></span>
									{t}
								</span>
							{/each}
							<span class="text-muted-foreground/70">· size = mentions · click a node for clips</span>
						</div>
					</div>
				{:else if view === 'table'}
					<div class="h-full overflow-auto p-3">
						{#if cypherResult?.error}
							<pre
								class="text-destructive border-destructive/40 bg-destructive/10 rounded-lg border p-3 text-xs whitespace-pre-wrap">{cypherResult.error}</pre>
						{:else if cypherResult && cypherResult.columns.length > 0}
							<table class="w-full text-left text-sm">
								<thead class="text-muted-foreground border-border border-b">
									<tr>
										{#each cypherResult.columns as c (c)}<th class="px-2 py-1 font-medium">{c}</th>{/each}
									</tr>
								</thead>
								<tbody>
									{#each cypherResult.rows as row, i (i)}
										<tr class="border-border/50 border-b">
											{#each row as cell, j (j)}
												<td class="px-2 py-1 align-top">{cell === null ? '—' : cell}</td>
											{/each}
										</tr>
									{/each}
								</tbody>
							</table>
							<p class="text-muted-foreground mt-2 text-xs">{cypherResult.rows.length} rows</p>
						{:else}
							<p class="text-muted-foreground text-sm">Run a Cypher query to see rows here.</p>
						{/if}
					</div>
				{:else}
					<pre class="h-full overflow-auto p-3 text-xs">{cypherResult
							? JSON.stringify(cypherResult.rows, null, 2)
							: 'Run a Cypher query to see JSON here.'}</pre>
				{/if}
			</div>

			{#if cypherOpen || view !== 'graph'}
				<aside class="border-border bg-card/20 w-80 shrink-0 space-y-2 overflow-auto border-l p-3">
					<div>
						<p class="text-foreground text-sm font-semibold">Example questions</p>
						<p class="text-muted-foreground text-xs">Click one to run it — results show on the left.</p>
					</div>
					{#each HELP_EXAMPLES as ex (ex.label)}
						<button
							class="border-border bg-background hover:border-primary/50 hover:bg-muted/60 block w-full space-y-1 rounded-lg border p-2.5 text-left transition-colors"
							onclick={() => runExample(ex.query)}
						>
							<span class="text-foreground block text-sm font-medium">{ex.label}</span>
							<span class="text-muted-foreground block text-xs leading-snug">{ex.desc}</span>
							<code
								class="text-muted-foreground/80 bg-muted/50 mt-1 block truncate rounded-md px-1.5 py-0.5 font-mono text-[0.7rem]"
								>{ex.query}</code
							>
						</button>
					{/each}
				</aside>
			{/if}
		</div>
	</div>

	<!-- ── entity side panel ──────────────────────────────────────────────── -->
	{#if detail?.entity}
		{@const e = detail.entity}
		<aside class="border-border bg-card/30 w-80 shrink-0 overflow-auto border-l p-3">
			<div class="flex items-start justify-between gap-2">
				<h2 class="text-foreground text-sm font-semibold break-words">{e.name}</h2>
				<button
					class="text-muted-foreground hover:text-foreground text-xs"
					onclick={() => (detail = null)}>✕</button
				>
			</div>
			<div class="text-muted-foreground mt-1 flex flex-wrap gap-1 text-xs">
				<span class="bg-secondary rounded-full px-2 py-0.5">{e.entity_type}</span>
				<span class="bg-secondary rounded-full px-2 py-0.5">{e.mention_count} mentions</span>
			</div>

			{#if detail.clips.length > 0}
				<h3 class="text-muted-foreground mt-4 mb-1 text-xs font-medium tracking-wide uppercase">
					Clips ({detail.clips.length})
				</h3>
				<ul class="flex flex-col gap-1">
					{#each detail.clips as c (c.chunk_id)}
						<li>
							<button
								class="hover:bg-muted/60 w-full rounded-md p-1.5 text-left"
								onclick={() => onClip(c.doc_id, c.start)}
								title="Open in player"
							>
								<div class="text-foreground flex items-center gap-1 text-xs">
									<span class="text-primary font-mono">{fmtTime(c.start)}</span>
									<span class="text-muted-foreground truncate">{c.title}</span>
								</div>
								{#if c.text}<p class="text-muted-foreground mt-0.5 line-clamp-2 text-xs">
										{c.text}
									</p>{/if}
							</button>
						</li>
					{/each}
				</ul>
			{/if}

			{#if detail.neighbors.length > 0}
				<h3 class="text-muted-foreground mt-4 mb-1 text-xs font-medium tracking-wide uppercase">
					Related
				</h3>
				<div class="flex flex-wrap gap-1">
					{#each detail.neighbors as nb (nb.entity_id + nb.direction)}
						<button
							class="border-border bg-secondary text-secondary-foreground hover:bg-muted/60 rounded-full border px-2 py-0.5 text-xs transition-colors"
							title={nb.description || nb.name}
							onclick={() => selectEntity(nb.entity_id)}
						>
							{nb.direction === 'out' ? '→ ' : '← '}{nb.name}
						</button>
					{/each}
				</div>
			{/if}

			{#if detail.cooccur.length > 0}
				<h3 class="text-muted-foreground mt-4 mb-1 text-xs font-medium tracking-wide uppercase">
					Co-occurs with
				</h3>
				<div class="flex flex-wrap gap-1">
					{#each detail.cooccur as co (co.entity_id)}
						<button
							class="border-border bg-secondary text-secondary-foreground hover:bg-muted/60 rounded-full border px-2 py-0.5 text-xs transition-colors"
							onclick={() => selectEntity(co.entity_id)}
						>
							{co.name} <span class="text-muted-foreground">·{co.shared}</span>
						</button>
					{/each}
				</div>
			{/if}
		</aside>
	{/if}
</div>
