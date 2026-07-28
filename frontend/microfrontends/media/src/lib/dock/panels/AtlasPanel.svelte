<script lang="ts">
	/**
	 * The EVōC embedding map as a dock panel.
	 *
	 * Same gate-then-dynamically-import shape as `routes/atlas/+page.svelte`: probe `/api/atlas/status`
	 * first (a dataset declaring no atlas spaces has no map and must not hit the endpoint at all), and
	 * only pull `mount-atlas.svelte` once a projection exists. The WebGPU context this eventually
	 * creates is precisely the kind of state a remount would destroy — and the renderer's mount-once
	 * invariant plus `defaultRenderer: 'always'` is what keeps it alive across a drag.
	 */
	import { onMount, type Component } from 'svelte';
	import { activeView, getAtlasStatus } from '@rask/media-api';

	type Phase = 'loading' | 'ready' | 'unavailable' | 'error';

	let phase = $state<Phase>('loading');
	let errorMsg = $state<string | null>(null);
	let Mount = $state.raw<Component | null>(null);

	// onMount, not the `$effect` + `browser` guard the /atlas ROUTE uses. This is a one-shot probe with
	// no reactive dependencies, and a panel only ever mounts client-side (the dock itself is created in
	// the host's onMount), so the guard has nothing to guard and the effect nothing to re-run on.
	onMount(() => {
		let cancelled = false;

		(async () => {
			try {
				const spaces = activeView().atlasSpaces;
				if (spaces.length === 0) {
					phase = 'unavailable';
					return;
				}
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
					Run <code class="bg-muted rounded-md px-1 py-0.5">ratch feature atlas</code> to build the 2-D projection
					of the chunks table.
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
