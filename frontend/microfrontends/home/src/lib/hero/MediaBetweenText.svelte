<script lang="ts">
	import type { Attachment } from 'svelte/attachments';
	import { gsap } from 'gsap';
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
</script>

<span class={cn('inline-flex flex-row items-center justify-center', className)}>
	<span>{firstText}</span>
	<span
		class="curtain mx-1 shrink-0 overflow-hidden rounded-xl sm:mx-2"
		style:--curtain-h="{targetWidth}px"
		{@attach openCurtain(targetWidth)}
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
