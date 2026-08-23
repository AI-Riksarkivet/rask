<script lang="ts">
	/**
	 * AtlasLegend — presentational legend / distribution panel for AtlasMap.
	 *
	 * Renders the two-branch legend (cluster vs categorical colour mode) with its
	 * clickable rows + distribution bars. Pure props-in / callbacks-out: it does
	 * NO data computation and never touches the cross-filter store directly —
	 * AtlasMap computes the rows (via ./atlas-legend) and threads the click/toggle
	 * handlers through, so the component stays dumb + testable.
	 */
	import { Eye, EyeOff } from '@lucide/svelte';
	import { Button } from '@rask/ui';
	import type { ClusterLegendRow, CategoryLegendRow } from './atlas-legend';
	import type { ColorBy } from './cross-filter.svelte';

	let {
		legendMode,
		clusterRows,
		categoryRows,
		clusterStats,
		categoryTitle,
		categoryTotal,
		legendFiltered,
		hidden,
		isDark,
		onPickCluster,
		onPickCategory,
		onToggleHidden,
		onShowAll,
	}: {
		legendMode: ColorBy;
		clusterRows: ClusterLegendRow[];
		categoryRows: CategoryLegendRow[];
		clusterStats: { total: number; noise: number };
		categoryTitle: string;
		categoryTotal: number;
		legendFiltered: boolean;
		hidden: ReadonlySet<number>;
		isDark: boolean;
		onPickCluster: (id: number) => void | Promise<void>;
		onPickCategory: (code: number) => void | Promise<void>;
		onToggleHidden: (code: number) => void;
		onShowAll: () => void;
	} = $props();
</script>

<!-- legend / distribution (clickable → select) -->
{#if legendMode === 'cluster' && clusterRows.length}
	<div
		class="border-border bg-card/85 absolute top-3 right-3 max-h-[60%] w-52 overflow-y-auto rounded-lg border p-2 text-xs shadow-sm backdrop-blur"
	>
		<div class="mb-1 flex items-center gap-2">
			<span class="text-muted-foreground font-medium"
				>Clusters · {clusterStats.total.toLocaleString()}</span
			>
			{#if hidden.size > 0}
				<Button
					variant="ghost"
					size="xs"
					class="text-primary ml-auto"
					onclick={() => onShowAll()}
					title="Show all hidden clusters"
				>
					Show all
				</Button>
			{/if}
		</div>
		{#each clusterRows as c (c.id)}
			{@const isHidden = hidden.has(c.id)}
			<div
				class="hover:bg-muted/60 relative flex w-full items-center gap-1 overflow-hidden rounded-md px-1 py-0.5"
				class:opacity-40={isHidden}
			>
				<span
					class="pointer-events-none absolute inset-y-0 left-0 rounded-sm"
					style:width="{(c.frac * 100).toFixed(1)}%"
					style:background={c.color}
					style:opacity="0.16"
				></span>
				<button
					type="button"
					class="relative flex flex-1 items-center gap-2 text-left"
					onclick={() => onPickCluster(c.id)}
					title="Select cluster #{c.id}"
				>
					<span class="size-2.5 shrink-0 rounded-full" style:background={c.color}></span>
					<span class="text-foreground">#{c.id}</span>
					<span class="text-muted-foreground ml-auto font-mono">{c.count.toLocaleString()}</span>
				</button>
				<button
					type="button"
					class="text-muted-foreground hover:text-foreground shrink-0 rounded-md p-0.5"
					onclick={() => onToggleHidden(c.id)}
					title={isHidden ? 'Show cluster on map' : 'Hide cluster on map'}
				>
					{#if isHidden}
						<EyeOff class="size-3.5" />
					{:else}
						<Eye class="size-3.5" />
					{/if}
				</button>
			</div>
		{/each}
		{#if clusterStats.noise > 0}
			{@const noiseHidden = hidden.has(-1)}
			<div
				class="border-border/60 hover:bg-muted/60 mt-1 flex w-full items-center gap-1 rounded-md border-t px-1 pt-1"
				class:opacity-40={noiseHidden}
			>
				<button
					type="button"
					class="text-muted-foreground flex flex-1 items-center gap-2 text-left"
					onclick={() => onPickCluster(-1)}
					title="Select the unclustered (noise) points"
				>
					<span
						class="size-2.5 shrink-0 rounded-full"
						style:background={isDark ? '#52525b' : '#cccccc'}
					></span>
					<span>noise (unclustered)</span>
					<span class="ml-auto font-mono">{clusterStats.noise.toLocaleString()}</span>
				</button>
				<button
					type="button"
					class="text-muted-foreground hover:text-foreground shrink-0 rounded-md p-0.5"
					onclick={() => onToggleHidden(-1)}
					title={noiseHidden ? 'Show noise on map' : 'Hide noise on map'}
				>
					{#if noiseHidden}
						<EyeOff class="size-3.5" />
					{:else}
						<Eye class="size-3.5" />
					{/if}
				</button>
			</div>
		{/if}
	</div>
{:else if categoryRows.length}
	<div
		class="border-border bg-card/85 absolute top-3 right-3 max-h-[60%] w-60 overflow-y-auto rounded-lg border p-2 text-xs shadow-sm backdrop-blur"
	>
		<div class="mb-1 flex items-center gap-2">
			<span class="text-muted-foreground font-medium">
				{categoryTitle} · {legendFiltered
					? `${categoryRows.length} of ${categoryTotal.toLocaleString()}`
					: categoryTotal.toLocaleString()}
			</span>
			{#if hidden.size > 0}
				<Button
					variant="ghost"
					size="xs"
					class="text-primary ml-auto"
					onclick={() => onShowAll()}
					title="Show all hidden"
				>
					Show all
				</Button>
			{/if}
		</div>
		{#each categoryRows as c (c.code)}
			{@const isHidden = hidden.has(c.code)}
			<div
				class="hover:bg-muted/60 relative flex w-full items-center gap-1 overflow-hidden rounded-md px-1 py-0.5"
				class:opacity-60={c.empty}
				class:opacity-40={isHidden}
			>
				<span
					class="pointer-events-none absolute inset-y-0 left-0 rounded-sm"
					style:width="{(c.frac * 100).toFixed(1)}%"
					style:background={c.color}
					style:opacity="0.16"
				></span>
				<button
					type="button"
					class="relative flex flex-1 items-center gap-2 text-left"
					onclick={() => onPickCategory(c.code)}
					title="Select {c.label}"
				>
					<span class="size-2.5 shrink-0 rounded-full" style:background={c.color}></span>
					<span class="text-foreground truncate" class:uppercase={legendMode === 'language'}
						>{c.label}</span
					>
					<span class="text-muted-foreground ml-auto shrink-0 font-mono"
						>{c.count.toLocaleString()}</span
					>
				</button>
				<button
					type="button"
					class="text-muted-foreground hover:text-foreground shrink-0 rounded-md p-0.5"
					onclick={() => onToggleHidden(c.code)}
					title={isHidden ? 'Show on map' : 'Hide on map'}
				>
					{#if isHidden}
						<EyeOff class="size-3.5" />
					{:else}
						<Eye class="size-3.5" />
					{/if}
				</button>
			</div>
		{/each}
	</div>
{/if}
