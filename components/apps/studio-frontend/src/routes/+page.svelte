<script lang="ts">
	import { base } from '$app/paths';
	import { Shapes, Sparkles, ArrowRight } from '@lucide/svelte';

	// Studio is the mini-apps playground. Each card is a self-contained experiment
	// built on this project's data, models, or — like the first one — the platform's
	// own UI tooling.
	const apps = [
		{
			href: `${base}/animation`,
			icon: Sparkles,
			title: 'Animation A/B',
			blurb: 'GSAP vs Svelte transitions — the same motion, two engines, side by side.',
		},
	];
</script>

<svelte:head><title>Studio — RASK</title></svelte:head>

<div class="mx-auto flex w-full max-w-3xl flex-col gap-5 p-6">
	<header class="flex items-center gap-3">
		<div
			class="bg-sidebar-primary text-sidebar-primary-foreground flex size-10 items-center justify-center rounded-xl"
		>
			<Shapes class="size-5" />
		</div>
		<div>
			<h1 class="text-xl font-semibold tracking-tight">Studio</h1>
			<p class="text-muted-foreground text-sm">
				Mini-applications built on this project's data and models.
			</p>
		</div>
	</header>

	<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
		{#each apps as app (app.href)}
			<a
				href={app.href}
				class="group bg-card hover:border-primary/50 flex flex-col rounded-xl border p-5 transition-colors"
			>
				<div
					class="bg-muted text-foreground mb-3 flex size-10 items-center justify-center rounded-lg"
				>
					<app.icon class="size-5" />
				</div>
				<div class="font-medium">{app.title}</div>
				<div class="text-muted-foreground text-sm">{app.blurb}</div>
				<div
					class="text-muted-foreground group-hover:text-foreground mt-4 flex items-center gap-1 text-sm"
				>
					Open <ArrowRight class="size-4" />
				</div>
			</a>
		{/each}

		<div
			class="border-border text-muted-foreground flex min-h-[164px] flex-col items-center justify-center gap-1 rounded-xl border border-dashed text-sm"
		>
			<Shapes class="size-5" />
			More mini-apps (soon)
		</div>
	</div>
</div>
