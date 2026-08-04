<script lang="ts">
	// Image/document viewer — the ra-anno PixiJS engine over a page image. WORKING.
	import { onDestroy } from 'svelte';
	import { loadAnnotations } from '@rask/labeling/annotations-client';
	import type { PixiContext } from '@rask/engine';
	import PixiCanvas from './PixiCanvas.svelte';
	import type { ViewerProps } from './types';

	let { unit, onload, onerror, controller }: ViewerProps = $props();

	// This component is remounted per unit (`{#key unit.key}` around the shell), and `onready` is
	// fired UN-AWAITED by PixiCanvas — so both of its awaits can outlive the component. Without a
	// liveness check the stale continuation re-armed a controller whose owning effect was already
	// destroyed: the engine kept calling closures that write its `$state`, and Svelte warned
	// `derived_inert` on every one. Checked after EACH await, because either can be the slow one.
	let alive = true;
	onDestroy(() => {
		alive = false;
		controller?.detach();
	});

	async function onready(ctx: PixiContext): Promise<void> {
		try {
			if (unit.imageUrl) await ctx.plugins.image.load(unit.imageUrl);
			if (!alive) return;
			// Hand the loaded still to the interaction layer so the CV tools
			// (magnetic corner-snap) can lazily build their corner maps when activated.
			ctx.plugins.interaction.setImageSource(ctx.plugins.image.imageElement);
			const { table, version } = await loadAnnotations(unit.annotationsUrl);
			if (!alive) return;
			ctx.plugins.arrow.load(table);
			ctx.plugins.arrow.sync();
			// Lift the engine + data to the route-level facade so the annotator layout
			// (toolbar/sidebar/zoom/layers) can bind to it. No-op when unwired. The
			// annotations URL doubles as the Save (POST) target; the version drives the
			// optimistic-concurrency handshake.
			controller?.attach(ctx, table, unit.annotationsUrl, version);
			onload?.(table.numRows);
		} catch (e) {
			// Surface the failure to the shell (status chip) — a hung "loading…" over a
			// 403/404/network error was the silent-failure mode this replaces. Swallowed
			// here (not rethrown): PixiCanvas fires onready un-awaited, so a rethrow
			// would only become an unhandled rejection.
			onerror?.(e instanceof Error ? e.message : String(e));
		}
	}
</script>

<PixiCanvas {onready} />
