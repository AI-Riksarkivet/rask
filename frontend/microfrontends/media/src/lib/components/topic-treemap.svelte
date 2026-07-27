<script lang="ts">
	/**
	 * Topic treemap (Tree page) with three views + a noise toggle.
	 *
	 * The LayerChart <Treemap> computes the rectangular layout from the topic
	 * hierarchy (`getTopics().hierarchy`); we draw the cells as SVG:
	 *
	 *   • "Flat" (drill)  — one level at a time. Click a branch to zoom in, a
	 *     breadcrumb to zoom out. The focused, low-clutter view.
	 *   • "Nested" — every level at once, children nested inside their parent
	 *     (parent label in the top strip). See sub-structure without drilling.
	 *   • "Flow" — a Sankey of the same hierarchy (<TopicSankey>): ribbon width
	 *     = chunk count, every level as a column.
	 *
	 * Clicking a leaf (or "Show results") opens the topic's results via
	 * `onShowResults` — the Tree page shows them in a side panel. The `(övrigt)`
	 * noise bucket (unclustered chunks) is hidden by default — toggle "Show
	 * noise" to bring it back (shown muted). Topic names are Swedish (the data);
	 * the chrome is English.
	 */
	import { Chart, Svg } from 'layerchart';
	import { Treemap } from 'layerchart/hierarchy';
	import { hierarchy as d3hierarchy, type HierarchyRectangularNode } from 'd3-hierarchy';
	import { scaleOrdinal } from 'd3-scale';
	import { schemeTableau10 } from 'd3-scale-chromatic';
	import { ChevronRight, ArrowRight, Eye, EyeOff } from '@lucide/svelte';
	import { Button } from '@rask/ui';
	import type { TopicNode } from '@rask/media-api';
	import TopicSankey from './topic-sankey.svelte';

	// `noiseLabel` is the unclustered-bucket name, sourced from /api/topics
	// (topic_tree.py:NOISE_LABEL) — never hardcoded here, so the two can't drift.
	let {
		hierarchy,
		noiseLabel,
		onShowResults,
	}: {
		hierarchy: TopicNode;
		noiseLabel: string;
		/** Open a topic's results (the page decides where — side panel). */
		onShowResults: (name: string) => void;
	} = $props();

	const fmt = (n: number) => n.toLocaleString('sv-SE');
	const sumLeaves = (d: TopicNode) => (d.children?.length ? 0 : (d.value ?? 0));

	let view = $state<'drill' | 'nested' | 'flow'>('drill');
	// The layout tabs, rendered as one segmented control in the header.
	const VIEW_TABS = [
		{ value: 'drill', label: 'Flat', title: 'One level at a time — click to zoom in' },
		{ value: 'nested', label: 'Nested', title: 'All levels nested at once' },
		{ value: 'flow', label: 'Flow', title: 'Sankey flow — ribbon width = chunk count' },
	] as const;
	let showNoise = $state(false);
	let hovered = $state<string | null>(null);

	// Drill stack: NAMES of the branches descended into below the root, resolved
	// against the `hierarchy` prop each time — never against the pruned display
	// copy, so the noise toggle (and its hidden-count hint) keeps working after a
	// drill. `current` is the deepest node shown.
	let drill = $state<string[]>([]);
	const path = $derived.by((): TopicNode[] => {
		const nodes: TopicNode[] = [hierarchy];
		for (const name of drill) {
			const next = nodes.at(-1)?.children?.find((c) => c.name === name);
			if (!next) break;
			nodes.push(next);
		}
		return nodes;
	});
	const current = $derived(path.at(-1) ?? hierarchy);

	/** Drop the noise bucket (recursively) so real topics fill the canvas. */
	function pruneNoise(node: TopicNode): TopicNode {
		if (!node.children) return node;
		return {
			...node,
			children: node.children.filter((c) => c.name !== noiseLabel).map(pruneNoise),
		};
	}

	const displayData = $derived(showNoise ? current : pruneNoise(current));

	// d3 hierarchy for the shown node: branches contribute 0 (leaves carry the
	// chunk count), so the treemap areas sum cleanly. Largest topics first.
	const root = $derived(
		d3hierarchy<TopicNode>(displayData)
			.sum(sumLeaves)
			.sort((a, b) => (b.value ?? 0) - (a.value ?? 0)),
	);

	// Chunks hidden by the noise toggle (for the breadcrumb hint).
	const hiddenCount = $derived.by(() => {
		if (showNoise) return 0;
		const full = d3hierarchy<TopicNode>(current).sum(sumLeaves);
		return (full.value ?? 0) - (root.value ?? 0);
	});

	// Nested view reserves a top strip per branch for its label; drill view doesn't.
	const pad = $derived(
		view === 'nested'
			? { paddingInner: 2, paddingOuter: 2, paddingTop: 16 }
			: { paddingInner: 3, paddingOuter: 0, paddingTop: 0 },
	);

	/** Fresh scale per displayed level, seeded with a deterministic domain (the
	 *  level's topics, largest-first — the render order). A component-lifetime
	 *  ordinal scale accumulates its domain across drills, so hues depended on
	 *  visit order and diverged from the Sankey's per-mount scale. */
	// Noise is excluded from the domain (it renders muted, never a scheme hue),
	// so toggling "Show noise" can't shift every real topic's colour by a slot.
	// MUST stay identical to the Sankey's topNames filter.
	const color = $derived(
		scaleOrdinal<string, string>(schemeTableau10).domain(
			(root.children ?? []).map((c) => c.data.name).filter((n) => n !== noiseLabel),
		),
	);
	/** A node's hue = its broadest (depth-1) ancestor, so a topic + its
	 *  sub-topics share a colour family across both views. */
	function nodeFill(node: HierarchyRectangularNode<TopicNode>): string {
		if (node.data.name === noiseLabel) return 'var(--color-muted)';
		const top = node.ancestors().find((n) => n.depth === 1);
		return color(top?.data.name ?? node.data.name);
	}

	const isInteractive = (node: TopicNode) => node.name !== noiseLabel;

	function onCell(node: HierarchyRectangularNode<TopicNode>) {
		if (!isInteractive(node.data)) return;
		if (node.data.children?.length) {
			// Push the FULL name path from the displayed root down to the clicked
			// branch: in Nested view a depth≥2 branch is NOT a direct child of
			// `current`, and pushing its bare name would never resolve in `path` —
			// worse, the unresolvable entry stayed in `drill` and silently broke
			// all further drilling until a reload.
			const names = node
				.ancestors()
				.reverse()
				.slice(1) // drop the displayed root (= `current`)
				.map((a) => a.data.name);
			drill = [...drill, ...names];
		} else {
			showResults(node.data.name);
		}
	}

	function showResults(name: string) {
		onShowResults(name);
	}

	/** Zoom out to breadcrumb position `index` (0 = root). */
	function crumbTo(index: number) {
		drill = drill.slice(0, index);
	}

	/** Trim a label to roughly fit a cell's width (no SVG text clipping). */
	function fit(name: string, width: number): string {
		const max = Math.floor((width - 12) / 7.2);
		if (max < 2) return '';
		return name.length > max ? `${name.slice(0, max - 1)}…` : name;
	}
</script>

<div class="flex h-full w-full flex-col">
	<!-- Breadcrumb + view/noise controls -->
	<div
		class="border-border bg-card/30 flex flex-wrap items-center gap-1 border-b px-4 py-2 text-xs"
	>
		{#each path as node, i (i)}
			{#if i > 0}<ChevronRight class="text-muted-foreground/50 size-3" />{/if}
			<Button
				variant="ghost"
				size="xs"
				class={i === path.length - 1
	? 'text-foreground disabled:cursor-default disabled:opacity-100'
	: 'text-muted-foreground'}
				disabled={i === path.length - 1}
				onclick={() => crumbTo(i)}
			>
				{node.name}
			</Button>
		{/each}
		<span class="text-muted-foreground/70 ml-1">
			· {fmt(root.value ?? 0)} chunks{#if hiddenCount > 0}
				· noise hidden: {fmt(hiddenCount)}{/if}
		</span>

		<div class="ml-auto flex items-center gap-2">
			<!-- noise toggle -->
			<Button
				variant="outline"
				size="xs"
				aria-pressed={showNoise}
				onclick={() => (showNoise = !showNoise)}
				title={showNoise ? 'Hide unclustered (övrigt)' : 'Show unclustered (övrigt)'}
			>
				{#if showNoise}<EyeOff /> Hide noise{:else}<Eye /> Show noise{/if}
			</Button>

			<!-- view switch (the estate segmented-control idiom) -->
			<div class="border-border bg-background flex items-center gap-0.5 rounded-lg border p-0.5">
				{#each VIEW_TABS as tab (tab.value)}
					<Button
						variant={view === tab.value ? 'secondary' : 'ghost'}
						size="xs"
						class={view === tab.value ? '' : 'text-muted-foreground'}
						aria-pressed={view === tab.value}
						onclick={() => (view = tab.value)}
						title={tab.title}
					>
						{tab.label}
					</Button>
				{/each}
			</div>

			{#if path.length > 1 && isInteractive(current)}
				<Button variant="secondary" size="xs" onclick={() => showResults(current.name)}>
					Show results <ArrowRight />
				</Button>
			{/if}
		</div>
	</div>

	<!-- Treemap / Sankey canvas -->
	<div class="min-h-0 flex-1 p-2 {view === 'flow' ? 'px-6' : ''}">
		{#if view === 'flow'}
			<TopicSankey data={displayData} {noiseLabel} onSelect={showResults} />
		{:else}
			<Chart data={root}>
				<Svg>
					<Treemap hierarchy={root} {...pad}>
						{#snippet children({ nodes }: { nodes: HierarchyRectangularNode<TopicNode>[] })}
							{@const visible =
								view === 'nested'
									? nodes.filter((n) => n.depth >= 1)
									: nodes.filter((n) => n.depth === 1)}
							{#each visible as node (node
								.ancestors()
								.map((a) => a.data.name)
								.join('›'))}
								{@const w = node.x1 - node.x0}
								{@const h = node.y1 - node.y0}
								{@const isBranch = !!node.data.children?.length}
								{@const isNoise = node.data.name === noiseLabel}
								{@const interactive = !isNoise}
								{@const header = view === 'nested' && isBranch}
								{@const label = fit(node.data.name, w)}
								<!-- Hover identity = the full path, NOT the name: topic names
                     repeat across branches (the noise bucket everywhere, some
                     sub-topics share their parent's name), and a name match
                     co-highlighted every same-named cell. -->
								{@const pathKey = node
									.ancestors()
									.map((a) => a.data.name)
									.join('›')}
								{@const hot = hovered === pathKey}
								<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
								<g
									role={interactive ? 'button' : undefined}
									tabindex={interactive ? 0 : undefined}
									class={interactive ? 'cursor-pointer' : 'cursor-not-allowed'}
									onclick={() => onCell(node)}
									onkeydown={(e) => {
	if (interactive && (e.key === 'Enter' || e.key === ' ')) {
		e.preventDefault();
		onCell(node);
	}
}}
									onmouseenter={() => (hovered = pathKey)}
									onmouseleave={() => (hovered = null)}
								>
									<title
										>{node.data.name} — {fmt(node.value ?? 0)} chunks{isBranch
											? ' (click to zoom in)'
											: interactive
												? ' (click to show results)'
												: ' (unclustered)'}</title
									>
									<rect
										x={node.x0}
										y={node.y0}
										width={w}
										height={h}
										rx={view === 'nested' ? 4 : 3}
										fill={nodeFill(node)}
										fill-opacity={isNoise ? 0.3 : header ? 0.4 : 1}
										opacity={hot ? 1 : 0.92}
										stroke="var(--color-background)"
										stroke-width={hot ? 2 : 1}
									/>
									{#if label && w > 44}
										{#if header}
											{#if h > 16}
												<text
													x={node.x0 + 6}
													y={node.y0 + 12}
													class="pointer-events-none fill-white text-xs font-semibold"
													style="paint-order: stroke; stroke: rgba(0,0,0,0.4); stroke-width: 2.5px;"
												>
													{label}
												</text>
											{/if}
										{:else if h > 22}
											<text
												x={node.x0 + 7}
												y={node.y0 + (view === 'nested' ? 13 : 17)}
												class="pointer-events-none fill-white {view === 'nested'
													? 'text-xs'
													: 'text-[12px]'} font-semibold"
												style="paint-order: stroke; stroke: rgba(0,0,0,0.35); stroke-width: 2.5px;"
											>
												{label}
											</text>
											{#if h > (view === 'nested' ? 28 : 38)}
												<text
													x={node.x0 + 7}
													y={node.y0 + (view === 'nested' ? 26 : 33)}
													class="pointer-events-none fill-white/85 text-[0.7rem] tabular-nums"
													style="paint-order: stroke; stroke: rgba(0,0,0,0.3); stroke-width: 2px;"
												>
													{fmt(node.value ?? 0)}
												</text>
											{/if}
										{/if}
									{/if}
								</g>
							{/each}
						{/snippet}
					</Treemap>
				</Svg>
			</Chart>
		{/if}
	</div>
</div>
