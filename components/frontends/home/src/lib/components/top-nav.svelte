<script lang="ts">
	import { base } from '$app/paths';
	import { Button } from '@rask/ui/button';
	import { gsap } from 'gsap';
	import { mode, toggleMode } from 'mode-watcher';
	import { Boxes, Sun, Moon, ArrowRight } from '@lucide/svelte';

	// App-local platform navbar — rendered ONLY by the catch-all host at `/` (the
	// pre-project home; the six domain MFEs render the @rask/ui AppShell sidebar
	// instead, never this bar). A floating, glassy pill on rask's own OKLCH tokens.

	// Subtle GSAP entrance (rask's {@attach} convention — see studio's gsapStagger).
	// The CENTERING transform lives on the OUTER wrapper, so GSAP can animate the pill's
	// y/opacity here without overwriting Tailwind's -translate-x-1/2. `clearProps` hands
	// the elements back to CSS once the entrance finishes (so hover transitions work).
	// Client-only (attachments run on mount); returns a cleanup that kills the timeline.
	function intro(node: HTMLElement) {
		const tl = gsap.timeline();
		tl.from(node, {
			y: -16,
			opacity: 0,
			duration: 0.55,
			ease: 'power3.out',
			clearProps: 'all',
		}).from(
			node.querySelectorAll('[data-nav-item]'),
			{ y: 6, duration: 0.4, stagger: 0.06, ease: 'power2.out', clearProps: 'all' },
			'-=0.3',
		);
		return () => tl.kill();
	}
</script>

<div class="fixed top-4 left-1/2 z-50 -translate-x-1/2">
	<header
		class="bg-background/70 border-border/60 flex h-14 items-center gap-3 rounded-full border px-2 pl-5 shadow-lg shadow-black/5 backdrop-blur-xl"
		{@attach intro}
	>
		<!-- BRAND: in-app soft nav home link (base === '' on the catch-all → '/'). -->
		<a href={base} data-nav-item class="group flex items-center gap-2">
			<Boxes class="text-primary size-5 transition-transform duration-300 group-hover:scale-110" />
			<span class="font-semibold tracking-tight">rask</span>
		</a>

		<div class="flex items-center gap-1">
			<!-- THEME TOGGLE: sleek icon, no dropdown. toggleMode is SSR-safe (handler-only). -->
			<Button
				data-nav-item
				variant="ghost"
				size="icon"
				class="rounded-full"
				aria-label="Toggle theme"
				onclick={toggleMode}
			>
				{#if mode.current === 'dark'}<Sun />{:else}<Moon />{/if}
			</Button>

			<!-- CTA: enter the workspace — cross-zone into overview → data-sveltekit-reload. -->
			<Button
				data-nav-item
				href="/overview"
				data-sveltekit-reload
				size="sm"
				class="rounded-full pr-3"
			>
				Open workspace
				<ArrowRight />
			</Button>
		</div>
	</header>
</div>
