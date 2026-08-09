<script lang="ts">
	// The RAINBOW button — the estate's one deliberately loud affordance, for entry points that
	// summon a MODEL (AI assist). An animated multi-hue border + soft glow says "this does
	// something different from every plain button around it" without a second layout; content and
	// behaviour are an ordinary <button>. Motion respects `prefers-reduced-motion`: the gradient
	// stands still, the shape and colours stay.
	import type { Snippet } from 'svelte';
	import type { HTMLButtonAttributes } from 'svelte/elements';

	let {
		children,
		class: className = '',
		...rest
	}: HTMLButtonAttributes & { children?: Snippet; class?: string } = $props();
</script>

<button class="rainbow-btn {className}" {...rest}>
	{@render children?.()}
</button>

<style>
	.rainbow-btn {
		--rainbow: linear-gradient(
			90deg,
			oklch(0.7 0.19 25),
			oklch(0.8 0.16 85),
			oklch(0.75 0.17 150),
			oklch(0.7 0.16 230),
			oklch(0.68 0.2 300),
			oklch(0.7 0.19 25)
		);
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		height: 2rem;
		padding: 0 0.75rem;
		border: 1px solid transparent;
		border-radius: calc(infinity * 1px);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		color: var(--foreground, inherit);
		/* Border-box paints the surface, border slot paints the moving rainbow ring. */
		background:
			linear-gradient(var(--background, Canvas), var(--background, Canvas)) padding-box,
			var(--rainbow) border-box;
		background-size:
			100% 100%,
			200% 100%;
		animation: rainbow-slide 3s linear infinite;
	}
	/* The GLOW: the same moving gradient, blurred, behind the button. */
	.rainbow-btn::before {
		content: '';
		position: absolute;
		inset: 0;
		z-index: -1;
		border-radius: inherit;
		background: var(--rainbow);
		background-size: 200% 100%;
		animation: rainbow-slide 3s linear infinite;
		filter: blur(0.6rem);
		opacity: 0.55;
	}
	.rainbow-btn:hover::before {
		opacity: 0.8;
	}
	.rainbow-btn:focus-visible {
		outline: 2px solid var(--ring, currentColor);
		outline-offset: 2px;
	}
	@keyframes rainbow-slide {
		0% {
			background-position:
				0 0,
				0% 0;
		}
		100% {
			background-position:
				0 0,
				200% 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.rainbow-btn,
		.rainbow-btn::before {
			animation: none;
		}
	}
</style>
