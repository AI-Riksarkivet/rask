<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn } from '@rask/ui/utils';

	// Blurred SVG blobs drifting along a seeded-random path — a soft aurora to sit behind a panel.
	// Ported from sv-animations' animated-gradient-svg; three things are rask's rather than the
	// reference's, and each is load-bearing:
	//
	//  - The `background-gradient` keyframes are DEFINED here. The reference inherits them from the
	//    library's global stylesheet, which this estate does not have. They must also be paired with
	//    the `animation-name` in the SAME scoped block: Svelte renames scoped keyframes, and a name
	//    written into an inline `style:animation` would keep the ORIGINAL name — matching nothing,
	//    animating nothing, silently.
	//  - There is no `useDimensions` hook here; `bind:clientWidth/clientHeight` is the SSR-safe
	//    equivalent (0 on the server, measured after hydration, re-measured on resize).
	//  - The blob colour goes through `style:fill`, not the `fill` presentation attribute, because
	//    callers pass raw token vars (`var(--primary)`) and `var()` inside a presentation attribute
	//    is not reliably supported.

	type BlurStrength = 'light' | 'medium' | 'heavy';

	interface Props extends HTMLAttributes<HTMLDivElement> {
		/** Any CSS <color>. Pass raw theme tokens so the blobs re-colour with light/dark. */
		colors: string[];
		/** Seconds for one full drift cycle. */
		speed?: number;
		blur?: BlurStrength;
		class?: string;
	}

	interface BlobConfig {
		color: string;
		widthFactor: number;
		heightFactor: number;
		top: string;
		left: string;
		tx1: number;
		ty1: number;
		tx2: number;
		ty2: number;
		tx3: number;
		ty3: number;
		tx4: number;
		ty4: number;
	}

	let { colors, speed = 5, blur = 'light', class: className, ...rest }: Props = $props();

	let width = $state(0);
	let height = $state(0);

	const circleSize = $derived(Math.max(width, height));
	const blurClass = $derived(
		blur === 'light' ? 'blur-2xl' : blur === 'medium' ? 'blur-3xl' : 'blur-[100px]',
	);

	function hashString(value: string): number {
		let hash = 2166136261;

		for (let index = 0; index < value.length; index += 1) {
			hash ^= value.charCodeAt(index);
			hash = Math.imul(hash, 16777619);
		}

		return hash >>> 0;
	}

	// Seeded, so a given colour list always draws the same arrangement — an SSR render and its
	// hydration must not disagree, and a re-render must not reshuffle the background.
	function createSeededRandom(seed: number): () => number {
		return () => {
			seed += 0x6d2b79f5;

			let t = seed;
			t = Math.imul(t ^ (t >>> 15), t | 1);
			t ^= t + Math.imul(t ^ (t >>> 7), t | 61);

			return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
		};
	}

	function randomBetween(random: () => number, min: number, max: number): number {
		return min + (max - min) * random();
	}

	const blobConfigs = $derived<BlobConfig[]>(
		colors.map((color, index) => {
			const random = createSeededRandom(hashString(`${color}-${index}`));
			// SPREAD ACROSS THE PANEL, don't let the seed decide coverage. The reference picks both
			// offsets from 0–50% of an element positioned by its top-left corner, so every blob's
			// CENTRE lands left-of-middle and above it — with three or four colours they pile into one
			// quadrant and the panel glows in a single corner while the rest reads flat (measured
			// in-browser: all the colour in the bottom-right, nothing on the left). Each blob now gets
			// its own lane along the width, and the seed only jitters it within that lane.
			const lane = colors.length > 1 ? index / (colors.length - 1) : 0.5;

			return {
				color,
				widthFactor: randomBetween(random, 0.5, 1.5),
				heightFactor: randomBetween(random, 0.5, 1.5),
				top: `${randomBetween(random, -20, 45)}%`,
				left: `${lane * 80 - 25 + randomBetween(random, -8, 8)}%`,
				tx1: randomBetween(random, -0.5, 0.5),
				ty1: randomBetween(random, -0.5, 0.5),
				tx2: randomBetween(random, -0.5, 0.5),
				ty2: randomBetween(random, -0.5, 0.5),
				tx3: randomBetween(random, -0.5, 0.5),
				ty3: randomBetween(random, -0.5, 0.5),
				tx4: randomBetween(random, -0.5, 0.5),
				ty4: randomBetween(random, -0.5, 0.5),
			};
		}),
	);
</script>

<div
	bind:clientWidth={width}
	bind:clientHeight={height}
	class={cn('absolute inset-0 overflow-hidden', className)}
	aria-hidden="true"
	{...rest}
>
	<div class={cn('absolute inset-0', blurClass)}>
		{#each blobConfigs as blob, index (index)}
			<svg
				class="blob absolute"
				width={circleSize * blob.widthFactor}
				height={circleSize * blob.heightFactor}
				viewBox="0 0 100 100"
				style:top={blob.top}
				style:left={blob.left}
				style:animation-duration="{speed}s"
				style:--tx-1={blob.tx1}
				style:--ty-1={blob.ty1}
				style:--tx-2={blob.tx2}
				style:--ty-2={blob.ty2}
				style:--tx-3={blob.tx3}
				style:--ty-3={blob.ty3}
				style:--tx-4={blob.tx4}
				style:--ty-4={blob.ty4}
			>
				<circle cx="50" cy="50" r="50" style:fill={blob.color} />
			</svg>
		{/each}
	</div>
</div>

<style>
	/* Only the duration is inline (it is a prop); the rest stays here so Svelte can rewrite the
	   keyframe name it scopes. */
	.blob {
		animation-name: background-gradient;
		animation-timing-function: ease-in-out;
		animation-iteration-count: infinite;
		will-change: transform;
	}

	/* Four drift points and back to the origin. Each blob carries its own --tx-N/--ty-N, so one set
	   of keyframes gives every blob a different path. */
	@keyframes background-gradient {
		0%,
		100% {
			transform: translate(0, 0);
		}
		20% {
			transform: translate(calc(var(--tx-1) * 100%), calc(var(--ty-1) * 100%));
		}
		40% {
			transform: translate(calc(var(--tx-2) * 100%), calc(var(--ty-2) * 100%));
		}
		60% {
			transform: translate(calc(var(--tx-3) * 100%), calc(var(--ty-3) * 100%));
		}
		80% {
			transform: translate(calc(var(--tx-4) * 100%), calc(var(--ty-4) * 100%));
		}
	}

	/* A background that never stops moving is exactly what the preference is for. */
	@media (prefers-reduced-motion: reduce) {
		.blob {
			animation: none;
		}
	}
</style>
