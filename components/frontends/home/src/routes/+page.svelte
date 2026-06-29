<script lang="ts">
	import { gsap } from 'gsap';
	import { Boxes } from '@lucide/svelte';
	import { getProjects } from '$lib/remote/home.remote';

	// Home / project picker — the pre-project landing at `/`. Projects come from the
	// operator (Project CRs) via the controlplane API through the gateway. Read-only:
	// projects are created with kubectl; opening a project is a later slice, so the
	// cards are not yet click-through.
	const projectsQuery = getProjects();
	// `await` suspends to the <svelte:boundary> pending snippet on first render
	// (svelte.config experimental.async). `.refresh()` could repoll later.
	const projects = $derived(await projectsQuery);

	// Map a Project.phase to a status-chip token class. Ready is the only "live"
	// state; everything mid-provision is muted; Failed is destructive.
	function phaseClass(phase: string): string {
		if (phase === 'Ready') return 'bg-primary/10 text-primary';
		if (phase === 'Failed') return 'bg-destructive/10 text-destructive';
		return 'bg-muted text-muted-foreground';
	}

	// Subtle GSAP stagger reveal of the hero. Client-only; targets static
	// [data-reveal] nodes present at attach time.
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
			Projects are provisioned by the platform operator. Each runs the image → ALTO pipeline in its
			own isolated workspace.
		</p>
	</header>

	<svelte:boundary>
		<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
			{#each projects as p (p.slug)}
				<div class="bg-card flex flex-col rounded-xl border p-5">
					<div
						class="bg-primary/10 text-primary mb-3 flex size-10 items-center justify-center rounded-lg"
					>
						<Boxes class="size-5" />
					</div>
					<div class="flex items-center justify-between gap-2">
						<div class="font-medium">{p.name}</div>
						<span class="rounded-full px-2 py-0.5 text-xs font-medium {phaseClass(p.phase)}">
							{p.phase}
						</span>
					</div>
					<div class="text-muted-foreground text-sm">{p.team} · {p.workload}</div>
				</div>
			{:else}
				<div
					class="border-border/70 text-muted-foreground col-span-full flex min-h-[164px] flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed text-sm"
				>
					No projects yet — create one with <code class="font-mono">kubectl apply</code>.
				</div>
			{/each}
		</div>

		{#snippet pending()}
			<div class="text-muted-foreground p-6">Loading projects…</div>
		{/snippet}

		{#snippet failed(error)}
			<div
				class="border-destructive/40 bg-destructive/10 text-destructive rounded-xl border p-4 text-sm"
			>
				Couldn't reach the platform: {error instanceof Error ? error.message : String(error)}
			</div>
		{/snippet}
	</svelte:boundary>
</div>
