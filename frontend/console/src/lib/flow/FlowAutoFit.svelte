<script lang="ts">
	// Rendered INSIDE <SvelteFlow> so it can use the flow context (svelte-flow rule 5).
	// Re-frames the viewport whenever the node-set signature (`trigger`) changes — i.e. when nodes are
	// added/removed or the view switches — but NOT on every data poll, so it never fights a user pan/zoom.
	import { useSvelteFlow, type FitViewOptions } from '@xyflow/svelte';
	import { tick } from 'svelte';
	import { on } from 'svelte/events';

	// `padding` is the caller's screen-space gutter (px per side) so the re-fit reserves the same
	// room for the floating overlays — toggle Panel, Controls, MiniMap — as the initial fitView.
	// `maxZoom` defaults to 1 — never scale a card ABOVE its natural size; a three-node estate used
	// to be blown up to fill the canvas, which read as "the graph is enormous". A caller whose canvas
	// is the page's main surface (the access explorer) may raise it a notch so a small graph does not
	// float in a sea of empty canvas.
	let {
		trigger,
		padding = 0.22,
		maxZoom = 1,
	}: { trigger: string; padding?: FitViewOptions['padding']; maxZoom?: number } = $props();
	const { fitView } = useSvelteFlow();

	$effect(() => {
		void trigger; // track the signature
		tick().then(() => fitView({ padding, duration: 400, maxZoom }));
	});

	/**
	 * Re-fit when the CONTAINER resizes, until the user takes over.
	 *
	 * `fitView` frames the graph against whatever the pane measured at that instant. Mount it somewhere
	 * that is sized LATER — a dock panel, an inactive tab, a collapsed section — and the fit is computed
	 * against a box that is empty or wrong, and the bad zoom then sticks forever. Measured in the
	 * lakehouse dock: six real nodes, a live renderer, and a viewport pinned at `scale(0.1)` — a graph
	 * that is present, correct and completely invisible, which reads as "the panel is broken".
	 *
	 * Re-fitting on resize is also just right on its own terms: dragging a dock sash is an explicit
	 * request for a different amount of room, and a graph should use it.
	 *
	 * The `trigger` effect above deliberately does NOT fight a user's pan/zoom, and neither does this:
	 * the first pointer or wheel gesture on the pane retires the observer for good. Until then the
	 * viewport is still the framing this component chose, so re-deriving it costs the user nothing.
	 */
	function refitOnResize(anchor: HTMLElement) {
		const pane = anchor.closest('.svelte-flow');
		if (!(pane instanceof HTMLElement)) return;

		let userDrives = false;
		let frame = 0;
		const surrender = () => {
			userDrives = true;
		};

		const observer = new ResizeObserver(() => {
			if (userDrives) return;
			// Coalesce a burst (a sash drag fires continuously) into one fit on the next frame, and
			// skip while the pane has no area — fitting against 0×0 is the bug this exists to avoid.
			cancelAnimationFrame(frame);
			frame = requestAnimationFrame(() => {
				if (userDrives || pane.clientWidth === 0 || pane.clientHeight === 0) return;
				void tick().then(() => fitView({ padding, duration: 0, maxZoom }));
			});
		});
		observer.observe(pane);
		// `capture` so a gesture counts even when a node or an overlay swallows it. `on` rather than
		// addEventListener: it returns its own unsubscribe, which is what the teardown below wants.
		const offPointer = on(pane, 'pointerdown', surrender, { capture: true });
		const offWheel = on(pane, 'wheel', surrender, { capture: true, passive: true });

		return () => {
			cancelAnimationFrame(frame);
			observer.disconnect();
			offPointer();
			offWheel();
		};
	}
</script>

<!-- A zero-box anchor: this component renders no UI, but it needs a node to find the flow pane from. -->
<span hidden {@attach refitOnResize}></span>
