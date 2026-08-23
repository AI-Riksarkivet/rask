<script lang="ts">
	/**
	 * Atlas tab — the EVōC embedding map over the Lance `chunks` table.
	 *
	 * Lance-native: the scatter is our custom WebGPU point renderer (<GpuScatter>),
	 * fed directly from our Lance backend over `/api/atlas/*` — NO DuckDB, NO
	 * Mosaic, NO parquet. Client-only (SPA, no SSR): we check
	 * `/api/atlas/status` first (which also reports which spaces are built), and
	 * only dynamically import the viewer (`mount-atlas.svelte` → <AtlasMap>) once
	 * a projection exists. If neither space is built, we point the user at the
	 * offline build step instead of failing. The same map lives in the search
	 * page's Map view, sharing the cross-filter store.
	 */
	import { browser } from '$app/environment';
	import type { Component } from 'svelte';
	import { activeView, getAtlasStatus } from '@rask/explorer-api';

	type Phase = 'loading' | 'ready' | 'unavailable' | 'error';

	let phase = $state<Phase>('loading');
	let errorMsg = $state<string | null>(null);
	let Mount = $state.raw<Component | null>(null);

	$effect(() => {
		if (!browser) return;
		let cancelled = false;

		(async () => {
			try {
				// A dataset that declares no atlas spaces has no map at all — show the
				// empty state without ever hitting /api/atlas (which would 404).
				const spaces = activeView().atlasSpaces;
				if (spaces.length === 0) {
					phase = 'unavailable';
					return;
				}
				// Probe the first declared space; the view is available if any declared
				// space is built (the in-map toggle gates the absent ones).
				const status = await getAtlasStatus(spaces[0]!.name);
				if (cancelled) return;
				const anyBuilt = status.projected || Object.values(status.spaces ?? {}).some(Boolean);
				if (!anyBuilt) {
					phase = 'unavailable';
					return;
				}
				const module = await import('$lib/atlas/mount-atlas.svelte');
				if (cancelled) return;
				Mount = module.default;
				phase = 'ready';
			} catch (e) {
				if (!cancelled) {
					errorMsg = e instanceof Error ? e.message : String(e);
					phase = 'error';
				}
			}
		})();

		return () => {
			cancelled = true;
		};
	});
</script>

<div class="h-full w-full">
	{#if phase === 'unavailable'}
		<div class="text-muted-foreground grid h-full place-items-center p-6 text-center text-sm">
			<div>
				<p class="text-foreground mb-1 font-medium">No embedding map yet</p>
				<p>
					Run <code class="bg-muted rounded-md px-1 py-0.5">ratch feature atlas</code> to build the 2-D
					projection of the chunks table.
				</p>
			</div>
		</div>
	{:else if phase === 'error'}
		<div class="text-destructive grid h-full place-items-center p-6 text-center text-sm">
			Failed to load Atlas: {errorMsg}
		</div>
	{:else if phase === 'ready' && Mount}
		<Mount />
	{:else}
		<div class="text-muted-foreground grid h-full place-items-center text-sm">Loading…</div>
	{/if}
</div>
