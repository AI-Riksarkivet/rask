<script lang="ts">
	import { gsap } from 'gsap';
	import { Boxes, Plus, ArrowRight, Users } from '@lucide/svelte';

	// Home / project picker — the pre-project landing at `/`, rendered with the floating
	// platform navbar (no sidebar). Projects aren't in the backend yet, so this is one
	// implicit "default" workspace; opening it enters the project at /<project>/overview.
	const projects = [{ name: 'Default', slug: 'default', subtitle: 'The default rask workspace' }];

	// Subtle GSAP stagger reveal of the hero + cards (rask's {@attach} convention, mirrors
	// studio's gsapStagger). `clearProps` hands each element back to CSS afterwards so the
	// card hover transitions aren't blocked by a leftover inline transform. Client-only.
	function reveal(node: HTMLElement) {
		const tween = gsap.from(node.querySelectorAll('[data-reveal]'), {
			y: 20,
			opacity: 0,
			duration: 0.6,
			stagger: 0.07,
			ease: 'power2.out',
			clearProps: 'all',
		});
		return () => tween.kill();
	}
</script>

<svelte:head><title>rask — HTR platform</title></svelte:head>

<div class="mx-auto w-full max-w-5xl px-6 pt-28 pb-20" {@attach reveal}>
	<header class="mb-12 max-w-2xl">
		<p data-reveal class="text-muted-foreground mb-3 font-mono text-xs tracking-[0.2em] uppercase">
			Riksarkivet · HTR platform
		</p>
		<h1 data-reveal class="text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
			Transcribe the archives.
		</h1>
		<p data-reveal class="text-muted-foreground mt-4 text-base leading-relaxed text-pretty">
			Open a project to run the image → ALTO pipeline — overview, compute, discover, storage, train
			and studio, each in its own workspace.
		</p>
	</header>

	<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
		{#each projects as p (p.slug)}
			<!-- data-sveltekit-reload: cross-zone into overview-frontend (hard nav). -->
			<a
				data-reveal
				href="/{p.slug}/overview"
				data-sveltekit-reload
				class="group bg-card hover:border-primary/50 flex flex-col rounded-xl border p-5 transition-colors hover:shadow-lg hover:shadow-black/5"
			>
				<div
					class="bg-primary/10 text-primary mb-3 flex size-10 items-center justify-center rounded-lg"
				>
					<Boxes class="size-5" />
				</div>
				<div class="font-medium">{p.name}</div>
				<div class="text-muted-foreground text-sm">{p.subtitle}</div>
				<div
					class="text-muted-foreground group-hover:text-foreground mt-4 flex items-center gap-1 text-sm transition-colors"
				>
					Open <ArrowRight class="size-4 transition-transform group-hover:translate-x-0.5" />
				</div>
			</a>
		{/each}

		<div
			data-reveal
			class="border-border/70 text-muted-foreground flex min-h-[164px] flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed text-sm"
		>
			<Plus class="size-5 opacity-70" />
			New project (soon)
		</div>

		<div
			data-reveal
			class="border-border/70 text-muted-foreground flex min-h-[164px] flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed text-sm"
		>
			<Users class="size-5 opacity-70" />
			Members &amp; access · RBAC (soon)
		</div>
	</div>
</div>
