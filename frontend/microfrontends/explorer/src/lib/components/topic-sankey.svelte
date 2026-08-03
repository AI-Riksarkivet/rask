<script lang="ts">
	/**
	 * Sankey ("Flow") view of the topic hierarchy — the third Tree-page view next
	 * to the flat/nested treemaps. Every hierarchy level is a column; ribbon
	 * width = the sub-topic's chunk count, so how a broad topic splits into
	 * sub-topics is readable at a glance. Clicking any (non-noise) topic opens
	 * its results, same as a treemap leaf.
	 *
	 * The chart data is PLAIN objects with index-based links (the canonical
	 * d3-sankey shape) — LayerChart's <Sankey> runs `structuredClone` on it
	 * before layout, which strips class prototypes, so d3-hierarchy nodes (and
	 * their .ancestors()/.find() methods) must not cross that boundary. The
	 * hierarchy walk happens here instead, stamping each node with what the
	 * template needs (key, colour key, branch flag).
	 */
	import { Chart, Svg, Link } from 'layerchart';
	import { Sankey } from 'layerchart/graph';
	import { hierarchy as d3hierarchy } from 'd3-hierarchy';
	import { scaleOrdinal } from 'd3-scale';
	import { schemeTableau10 } from 'd3-scale-chromatic';
	import type { TopicNode } from '@rask/explorer-api';

	let {
		data,
		noiseLabel,
		onSelect,
	}: {
		/** The (already noise-pruned, drill-scoped) hierarchy to lay out. */
		data: TopicNode;
		noiseLabel: string;
		/** Open a topic's results (any non-noise node). */
		onSelect: (name: string) => void;
	} = $props();

	/** Plain per-topic datum fed to the chart (survives structuredClone). */
	type FlowDatum = {
		key: string;
		name: string;
		depth: number;
		isBranch: boolean;
		/** Broadest (depth-1) ancestor name — drives the shared colour family. */
		colorKey: string;
		noise: boolean;
	};
	/** A datum after d3-sankey has stamped its layout onto it. */
	type FlowNode = FlowDatum & { x0: number; x1: number; y0: number; y1: number; value: number };
	type FlowLink = { source: FlowNode; target: FlowNode; value: number; width?: number };

	const fmt = (n: number) => n.toLocaleString('sv-SE');
	const sumLeaves = (d: TopicNode) => (d.children?.length ? 0 : (d.value ?? 0));

	// Same sum + largest-first sort as the treemap, so the ordinal colour scale
	// sees topics in the same order and the two views share hues.
	const graph = $derived.by(() => {
		const root = d3hierarchy<TopicNode>(data)
			.sum(sumLeaves)
			.sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
		const ordered = root.descendants();
		const indexOf = new Map(ordered.map((n, i) => [n, i]));
		const nodes: FlowDatum[] = ordered.map((n) => ({
			key: n
				.ancestors()
				.map((a) => a.data.name)
				.reverse()
				.join('›'),
			name: n.data.name,
			depth: n.depth,
			isBranch: !!n.children?.length,
			colorKey: n.ancestors().find((a) => a.depth === 1)?.data.name ?? n.data.name,
			noise: n.data.name === noiseLabel,
		}));
		const links = root.links().map((l) => ({
			source: indexOf.get(l.source)!,
			target: indexOf.get(l.target)!,
			value: l.target.value ?? 0,
		}));
		// Depth-1 names largest-first, noise excluded (it renders muted) — the
		// treemap seeds its scale with the same ordered+filtered domain, so the
		// two views can't drift apart and the noise toggle can't shift hues.
		const topNames = (root.children ?? []).map((c) => c.data.name).filter((n) => n !== noiseLabel);
		return { nodes, links, topNames };
	});

	const color = $derived(scaleOrdinal<string, string>(schemeTableau10).domain(graph.topNames));
	function nodeFill(node: FlowNode): string {
		return node.noise ? 'var(--color-muted)' : color(node.colorKey);
	}

	const isInteractive = (node: FlowNode) => node.depth > 0 && !node.noise;
	let hovered = $state<string | null>(null);

	/** Vertical room per leaf row. d3-sankey does NOT shrink its padding to fit:
	 *  once a column's padding alone exceeds the canvas height, the scale goes
	 *  negative and every node collapses to 0px. So the canvas grows with the
	 *  leaf count and the wrapper scrolls instead. */
	const ROW_PX = 9;
	const MIN_HEIGHT_PX = 560;
	const canvasHeight = $derived.by(() => {
		let leaves = 0;
		(function count(n: TopicNode): void {
			if (n.children?.length) n.children.forEach(count);
			else leaves += 1;
		})(data);
		return Math.max(MIN_HEIGHT_PX, leaves * ROW_PX);
	});
</script>

{#if graph.links.length === 0}
	<div class="text-muted-foreground grid h-full place-items-center text-xs">
		Nothing to flow here — this topic has no (non-noise) sub-topics.
	</div>
{:else}
	<div class="h-full overflow-y-auto">
		<div style="height: {canvasHeight}px">
			<Chart data={graph}>
				<Svg>
					<Sankey nodeWidth={10} nodePadding={6} nodeAlign="justify">
						{#snippet children({ nodes, links }: { nodes: FlowNode[]; links: FlowLink[] })}
							{#each links as link (`${link.source.key}→${link.target.key}`)}
								<Link
									sankey
									data={link}
									fill="none"
									stroke={nodeFill(link.target)}
									stroke-opacity={hovered === link.target.key ? 0.65 : 0.3}
									stroke-width={Math.max(1, link.width ?? 0)}
								/>
							{/each}
							{#each nodes as node (node.key)}
								{@const h = node.y1 - node.y0}
								{@const interactive = isInteractive(node)}
								<!-- Hover identity = the unique path key, not the name — topic
                     names repeat across branches and co-highlighted. -->
								{@const hot = hovered === node.key}
								<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
								<g
									role={interactive ? 'button' : undefined}
									tabindex={interactive ? 0 : undefined}
									class={interactive ? 'cursor-pointer' : undefined}
									onclick={() => interactive && onSelect(node.name)}
									onkeydown={(e) => {
	if (interactive && (e.key === 'Enter' || e.key === ' ')) {
		e.preventDefault();
		onSelect(node.name);
	}
}}
									onmouseenter={() => (hovered = node.key)}
									onmouseleave={() => (hovered = null)}
								>
									<title
										>{node.name} — {fmt(node.value ?? 0)} chunks{interactive
											? ' (click to show results)'
											: ''}</title
									>
									<rect
										x={node.x0}
										y={node.y0}
										width={node.x1 - node.x0}
										height={Math.max(1, h)}
										rx={2}
										fill={nodeFill(node)}
										opacity={hot ? 1 : 0.9}
									/>
									{#if h > 11}
										<!-- Leaves sit on the right edge (justify): label to the left. -->
										<text
											x={node.isBranch ? node.x1 + 5 : node.x0 - 5}
											y={(node.y0 + node.y1) / 2}
											dy="0.32em"
											text-anchor={node.isBranch ? 'start' : 'end'}
											class="fill-foreground pointer-events-none text-[0.7rem]"
										>
											{node.name}
											<tspan class="fill-muted-foreground tabular-nums">
												{fmt(node.value ?? 0)}</tspan
											>
										</text>
									{/if}
								</g>
							{/each}
						{/snippet}
					</Sankey>
				</Svg>
			</Chart>
		</div>
	</div>
{/if}
