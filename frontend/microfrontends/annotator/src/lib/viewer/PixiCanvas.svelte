<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount, setContext } from 'svelte';
	import { Application } from 'pixi.js';
	import { ArrowDataPlugin } from '@rask/engine';
	import { ImagePlugin } from '@rask/engine';
	import { InteractionManager } from '@rask/engine';
	import type { PixiContext } from '@rask/engine';
	import type { Snippet } from 'svelte';

	let {
		children,
		zoom = $bindable(1),
		panX = $bindable(0),
		panY = $bindable(0),
		colorFn,
		annotationStyle,
		onready,
	}: {
		children?: Snippet;
		zoom?: number;
		panX?: number;
		panY?: number;
		colorFn?: (status: string) => number;
		annotationStyle?: {
			fillAlpha?: number;
			strokeWidth?: number;
			strokeAlpha?: number;
		};
		onready?: (ctx: PixiContext) => void;
	} = $props();

	let containerEl: HTMLDivElement;
	let ready = $state(false);

	const pixiCtx: {
		app: Application | null;
		plugins: {
			image: ImagePlugin | null;
			arrow: ArrowDataPlugin | null;
			interaction: InteractionManager | null;
		};
	} = {
		app: null,
		plugins: { image: null, arrow: null, interaction: null },
	};
	setContext('pixi', pixiCtx);

	onMount(() => {
		if (!browser) return;

		const app = new Application();
		let imagePlugin: ImagePlugin;
		let arrowPlugin: ArrowDataPlugin;
		let interaction: InteractionManager;
		let resizeObs: ResizeObserver | null = null;
		let initialized = false;
		let destroyed = false;

		(async () => {
			// Do NOT use resizeTo — we handle sizing ourselves via ResizeObserver
			// to ensure re-render happens when ticker is stopped.
			const { width, height } = containerEl.getBoundingClientRect();
			await app.init({
				width: Math.max(width, 1),
				height: Math.max(height, 1),
				preference: 'webgpu',
				backgroundAlpha: 0,
				background: 'transparent',
				antialias: true,
				resolution: window.devicePixelRatio || 1,
				autoDensity: true,
			});

			if (destroyed) return;
			// Constrain canvas to its container — critical for PaneForge resizable panes
			const canvas = app.canvas as HTMLCanvasElement;
			canvas.style.display = 'block';
			canvas.style.maxWidth = '100%';
			canvas.style.maxHeight = '100%';
			// eslint-disable-next-line svelte/no-dom-manipulating -- Pixi owns this canvas; it is mounted into a plain container div Svelte never renders children into
			containerEl.appendChild(canvas);
			initialized = true;

			// Stop continuous 60fps rendering — render on demand only
			app.ticker.stop();

			// Watch container for size changes — resize renderer + re-render
			resizeObs = new ResizeObserver((entries) => {
				if (destroyed || !initialized) return;
				const entry = entries[0];
				if (!entry) return;
				const { width: w, height: h } = entry.contentRect;
				if (w > 0 && h > 0) {
					app.renderer.resize(w, h);
					app.render();
				}
			});
			resizeObs.observe(containerEl);

			imagePlugin = new ImagePlugin(app);
			arrowPlugin = new ArrowDataPlugin(app, colorFn, annotationStyle);
			interaction = new InteractionManager(app, arrowPlugin);

			imagePlugin.canPan = () => interaction.currentTool === 'pan';

			imagePlugin.onViewportChange = (bounds) => {
				zoom = imagePlugin.zoom;
				panX = imagePlugin.panX;
				panY = imagePlugin.panY;
				arrowPlugin.setViewport(bounds);
				interaction.updateHandles();
			};

			pixiCtx.app = app;
			pixiCtx.plugins.image = imagePlugin;
			pixiCtx.plugins.arrow = arrowPlugin;
			pixiCtx.plugins.interaction = interaction;
			ready = true;

			onready?.({
				app,
				plugins: { image: imagePlugin, arrow: arrowPlugin, interaction },
			});
		})();

		return () => {
			destroyed = true;
			resizeObs?.disconnect();
			interaction?.destroy();
			arrowPlugin?.destroy();
			imagePlugin?.destroy();
			if (initialized) {
				app.destroy(true, { children: true });
			}
		};
	});
</script>

<div
	bind:this={containerEl}
	class="pixi-grid relative h-full w-full overflow-hidden"
	style="cursor: default;"
>
	{#if ready && children}
		{@render children()}
	{/if}
</div>
