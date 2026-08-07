<script lang="ts">
	import type { Attachment } from 'svelte/attachments';
	import { gsap } from 'gsap';
	import { on } from 'svelte/events';
	import { cn } from '@rask/ui/utils';

	// A wordmark that OPENS around a piece of media: `firstText`, a curtain that widens from nothing
	// to its target, then `secondText`. Ported from the sv-animations component of the same name —
	// its spring lives in motion/framer, which this estate does not carry, so the tween is GSAP's
	// `power4.out`: the reference asked for a spring with `bounce: 0`, i.e. settle with NO overshoot.
	//
	// The root is a <span> and every part of the lockup is phrasing content, so the whole thing can
	// sit inside an <h1> without producing invalid markup.

	interface Props {
		firstText: string;
		secondText: string;
		mediaSrc: string;
		/** Leave empty when the media is decoration — anything here joins the wordmark's accessible name. */
		alt?: string;
		/** Open width in px at >= 640px; below that the curtain opens to SMALL_FACTOR of it. */
		targetWidth?: number;
		class?: string;
	}

	let {
		firstText,
		secondText,
		mediaSrc,
		alt = '',
		targetWidth = 100,
		class: className,
	}: Props = $props();

	// The reference's two widths were 100px / 40px. Kept as a ratio so `targetWidth` stays one prop
	// and the small-screen size cannot drift away from it.
	const SMALL_FACTOR = 0.4;

	// Width is ANIMATED rather than declared, and the closed state lives in CSS below rather than in
	// a `gsap.from()`: `from` renders its start values only after the attachment runs, so the open
	// state would paint for a frame and then snap shut. matchMedia owns both the breakpoint and the
	// reduced-motion branch, and reverts each when its query stops matching — which is also what
	// replays the tween at the other width when the viewport crosses 640px.
	//
	// `small` and `large` MUST tile the whole range. gsap.matchMedia runs its callback only while at
	// least ONE of its queries matches, so a lone `small` + `reduce` pair leaves a wide viewport with
	// no reduced-motion preference matching nothing at all — the callback never fires and the curtain
	// stays shut at width 0. That is why GSAP's own docs pair isDesktop with isMobile rather than
	// testing one side. `reduce` only selects the branch; it is never the thing that triggers it.
	function openCurtain(target: number): Attachment<HTMLElement> {
		return (node) => {
			const mm = gsap.matchMedia();
			mm.add(
				{
					small: '(max-width: 639px)',
					large: '(min-width: 640px)',
					reduce: '(prefers-reduced-motion: reduce)',
				},
				(ctx) => {
					const { small = false, reduce = false } = ctx.conditions ?? {};
					const width = small ? Math.round(target * SMALL_FACTOR) : target;
					if (reduce) {
						gsap.set(node, { width });
						return;
					}
					gsap.fromTo(node, { width: 0 }, { width, duration: 1, delay: 0.1, ease: 'power4.out' });
				},
			);
			return () => mm.revert();
		};
	}

	// HOVER. The curtain is the only playful thing on an otherwise sober landing, so it answers the
	// pointer: a small lift + tilt on enter, settled back on leave. `overwrite: 'auto'` because a fast
	// in-out-in must not queue three tweens on one transform, and the pair is registered only when
	// motion is allowed — under `reduce` the element simply does not respond, which is the correct
	// non-answer rather than an instant jump.
	const hoverLift: Attachment<HTMLElement> = (node) => {
		const mm = gsap.matchMedia();
		mm.add('(prefers-reduced-motion: no-preference)', () => {
			const to = (vars: gsap.TweenVars) =>
				gsap.to(node, { duration: 0.35, overwrite: 'auto', ...vars });
			// `on` from svelte/events, not addEventListener: it returns its own unsubscriber, so the
			// two listeners cannot drift out of sync with their removals (the estate lints for this).
			const offEnter = on(node, 'pointerenter', () =>
				to({ y: -6, rotate: -4, scale: 1.12, ease: 'back.out(2.5)' }),
			);
			const offLeave = on(node, 'pointerleave', () =>
				to({ y: 0, rotate: 0, scale: 1, ease: 'power2.out' }),
			);
			return () => {
				offEnter();
				offLeave();
			};
		});
		return () => mm.revert();
	};
</script>

<span class={cn('inline-flex flex-row items-center justify-center', className)}>
	<span>{firstText}</span>
	<span
		class="curtain mx-1 shrink-0 cursor-pointer overflow-hidden rounded-lg sm:mx-1.5"
		style:--curtain-h="{targetWidth}px"
		{@attach openCurtain(targetWidth)}
		{@attach hoverLift}
	>
		<!-- object-cover is what makes the widening read as a REVEAL rather than a horizontal squash:
		     the image keeps its aspect ratio and the box crops it. -->
		<img class="h-full w-full object-cover" src={mediaSrc} {alt} />
	</span>
	<span>{secondText}</span>
</span>

<style>
	/* Closed until the attachment opens it. Height is fixed so the line does not reflow while the
	   width tweens, and steps at the same breakpoint and ratio the width does. */
	.curtain {
		width: 0;
		height: calc(var(--curtain-h) * 0.4);
	}

	@media (min-width: 640px) {
		.curtain {
			height: var(--curtain-h);
		}
	}
</style>
