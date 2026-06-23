<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { gsap } from 'gsap';
	import { fly } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { ArrowLeft, Play } from '@lucide/svelte';

	type Engine = 'gsap' | 'svelte';
	const ENGINES = [
		{ id: 'gsap', label: 'GSAP' },
		{ id: 'svelte', label: 'Svelte transitions' },
	] as const;

	let engine = $state<Engine>('gsap');
	let runId = $state(0); // bump → the {#key} block remounts → both engines replay
	// Only animate after mount: the SSR'd markup isn't animated, so the keyed block
	// is created client-side and BOTH engines play their entrance on first paint
	// (no hydration asymmetry, no double-play flicker).
	let mounted = $state(false);
	onMount(() => (mounted = true));

	const items = [
		{ label: 'Layout', hue: 'bg-sky-500/15 text-sky-400' },
		{ label: 'Lines', hue: 'bg-emerald-500/15 text-emerald-400' },
		{ label: 'Transcribe', hue: 'bg-amber-500/15 text-amber-400' },
		{ label: 'ALTO', hue: 'bg-violet-500/15 text-violet-400' },
		{ label: 'Index', hue: 'bg-rose-500/15 text-rose-400' },
		{ label: 'Search', hue: 'bg-cyan-500/15 text-cyan-400' },
	];

	// GSAP entrance via the Svelte 5 attachment API. Re-runs every time the {#key}
	// block remounts this element (engine switch / replay) — the reactive win over
	// legacy `use:` actions. Returns a cleanup that kills the tween.
	function gsapStagger(node: HTMLElement) {
		const cards = node.querySelectorAll('[data-card]');
		const tween = gsap.from(cards, {
			y: 28,
			opacity: 0,
			scale: 0.96,
			duration: 0.5,
			stagger: 0.08,
			ease: 'power2.out',
		});
		return () => tween.kill();
	}

	const replay = () => (runId += 1);
	function pick(id: Engine) {
		engine = id;
		replay();
	}
</script>

<svelte:head><title>Animation A/B — Studio</title></svelte:head>

<div class="mx-auto flex w-full max-w-3xl flex-col gap-5 p-6">
	<a
		href={base}
		class="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-sm"
	>
		<ArrowLeft class="size-4" /> Studio
	</a>

	<header>
		<h1 class="text-xl font-semibold tracking-tight">Animation A/B — GSAP vs Svelte</h1>
		<p class="text-muted-foreground mt-1 text-sm">
			The same stagger reveal, driven two ways: GSAP via the Svelte 5 <code class="font-mono"
				>{'{@attach}'}</code
			>
			API, and the native side via <code class="font-mono">svelte/transition</code>. (Route-level
			transitions across the app use the View Transitions API.) Flip the engine and hit Replay to
			compare.
		</p>
	</header>

	<div class="flex items-center gap-3">
		<div class="bg-muted inline-flex rounded-lg p-0.5">
			{#each ENGINES as e (e.id)}
				<button
					class="rounded-md px-3 py-1 text-sm transition-colors {engine === e.id
						? 'bg-background shadow-sm'
						: 'text-muted-foreground hover:text-foreground'}"
					onclick={() => pick(e.id)}
				>
					{e.label}
				</button>
			{/each}
		</div>
		<Button size="sm" variant="outline" onclick={replay}>
			<Play class="size-3.5" /> Replay
		</Button>
	</div>

	{#if mounted}
		{#key `${engine}-${runId}`}
			{#if engine === 'gsap'}
				<div class="grid grid-cols-2 gap-3 sm:grid-cols-3" {@attach gsapStagger}>
					{#each items as it (it.label)}
						<div data-card>
							<Card class="flex h-24 items-center justify-center text-sm font-medium {it.hue}">
								{it.label}
							</Card>
						</div>
					{/each}
				</div>
			{:else}
				<div class="grid grid-cols-2 gap-3 sm:grid-cols-3">
					{#each items as it, i (it.label)}
						<div in:fly={{ y: 28, duration: 500, delay: i * 80, easing: cubicOut }}>
							<Card class="flex h-24 items-center justify-center text-sm font-medium {it.hue}">
								{it.label}
							</Card>
						</div>
					{/each}
				</div>
			{/if}
		{/key}
	{:else}
		<!-- SSR / pre-mount placeholder (no animation; the client mount plays the entrance). -->
		<div class="grid grid-cols-2 gap-3 sm:grid-cols-3" aria-hidden="true">
			{#each items as it (it.label)}
				<Card class="flex h-24 items-center justify-center text-sm font-medium opacity-0 {it.hue}">
					{it.label}
				</Card>
			{/each}
		</div>
	{/if}
</div>
