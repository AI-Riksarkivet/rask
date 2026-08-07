<script lang="ts">
	import type { Attachment } from 'svelte/attachments';
	import { gsap } from 'gsap';
	import { cn } from '@rask/ui/utils';

	// A field of dots, drawn once as an SVG <pattern> and tiled by the browser — the whole background
	// is ONE <rect>, not N circles, so the DOM cost does not grow with the panel. Ported from the
	// sv-animations component of the same name; the reference's glow is a per-dot fade, which here is
	// a small pool of overlay dots tweened by GSAP rather than a CSS animation per element.
	//
	// THEME-AWARE BY CONSTRUCTION: every dot is `fill="currentColor"`, so the colour comes from the
	// `text-*` utility a caller sets and re-resolves under `.dark` with no second code path. This is
	// why the component takes no colour prop — a prop would be a second source of truth that could
	// only go stale against the tokens.

	interface Props {
		/** Horizontal spacing between dots. */
		width?: number;
		/** Vertical spacing between dots. */
		height?: number;
		/** Offset of the whole pattern. */
		x?: number;
		y?: number;
		/** Offset of a dot inside its cell. */
		cx?: number;
		cy?: number;
		/** Dot radius. */
		cr?: number;
		/** Drift a few dots in and out, slowly. Ignored under prefers-reduced-motion. */
		glow?: boolean;
		class?: string;
	}

	let {
		width = 16,
		height = 16,
		x = 0,
		y = 0,
		cx = 1,
		cy = 1,
		cr = 1,
		glow = false,
		class: className,
	}: Props = $props();

	// The pattern id must be unique per instance or two patterns on one page resolve to whichever
	// <defs> the browser saw last. `$props.id()` is Svelte's own per-instance id — SSR-stable, so the
	// server and client markup agree and hydration does not swap the fill out from under the paint.
	const uid = $props.id();
	const patternId = `dots-${uid}`;

	//: How many dots the glow pool animates. Small on purpose: the effect is meant to be noticed only
	//: as a faint shimmer, and each one is an independent tween on the main thread.
	const GLOW_DOTS = 14;

	// Seeded so the layout is identical on server and client — Math.random() here would place the
	// dots differently in the SSR markup than in the hydrated DOM, which paints as a jump.
	function seeded(seed: number): () => number {
		let s = seed;
		return () => {
			s = (s * 1664525 + 1013904223) % 4294967296;
			return s / 4294967296;
		};
	}

	const glowDots = $derived.by(() => {
		const random = seeded(20260807);
		return Array.from({ length: GLOW_DOTS }, (_, i) => ({
			id: i,
			left: `${Math.round(random() * 100)}%`,
			top: `${Math.round(random() * 100)}%`,
			delay: random() * 6,
		}));
	});

	// Each glow dot breathes on its own clock: a yoyo tween with a per-dot delay, so the field never
	// pulses in unison (which reads as a flash rather than a shimmer). One matchMedia gate for the
	// whole pool — under `reduce` no tween is created at all and the dots stay at their resting
	// opacity, which is the correct still image rather than a frozen mid-animation frame.
	const breathe: Attachment<SVGElement> = (node) => {
		const mm = gsap.matchMedia();
		mm.add('(prefers-reduced-motion: no-preference)', () => {
			const dots = node.querySelectorAll('[data-glow-dot]');
			const tweens = [...dots].map((dot, i) =>
				gsap.to(dot, {
					opacity: 0.9,
					duration: 2.4,
					delay: Number((dot as SVGElement).dataset.delay ?? i * 0.3),
					repeat: -1,
					yoyo: true,
					ease: 'sine.inOut',
				}),
			);
			return () => tweens.forEach((t) => t.kill());
		});
		return () => mm.revert();
	};
</script>

<!-- `currentColor` + the caller's text utility is the whole theming story; `pointer-events-none`
     because this is decoration and must never eat a click meant for the content above it. -->
<svg
	class={cn('pointer-events-none absolute inset-0 h-full w-full', className)}
	aria-hidden="true"
	{@attach glow ? breathe : () => {}}
>
	<defs>
		<pattern
			id={patternId}
			{width}
			{height}
			patternUnits="userSpaceOnUse"
			patternContentUnits="userSpaceOnUse"
			{x}
			{y}
		>
			<circle {cx} {cy} r={cr} fill="currentColor" />
		</pattern>
	</defs>
	<rect width="100%" height="100%" fill="url(#{patternId})" />
	{#if glow}
		{#each glowDots as dot (dot.id)}
			<circle
				data-glow-dot
				data-delay={dot.delay}
				cx={dot.left}
				cy={dot.top}
				r={cr * 1.6}
				fill="currentColor"
				opacity="0.15"
			/>
		{/each}
	{/if}
</svg>
