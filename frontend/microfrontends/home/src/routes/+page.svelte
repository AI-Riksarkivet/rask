<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { Activity, Boxes, FolderKanban, Settings } from '@lucide/svelte';
	import type { PageData } from './$types';

	// HOME — the estate landing, and the first of the main menu's three places (Home · Projects ·
	// Settings). It is NOT the project gallery any more: that surface is `/projects`, where its
	// "you are not a member of any project yet" empty state belongs. While `/` WAS the gallery, a
	// person with no memberships got that one sentence as their entire product, which is the wrong
	// answer to "what is this, and what is happening in it".
	//
	// SCAFFOLD, and it says so. The insights this page will carry are real questions — what ran, what
	// landed, what needs attention — but each needs a read that does not exist yet, and inventing
	// plausible numbers on a landing page is worse than admitting there are none. Same treatment the
	// `train` zone uses for the same reason: real structure, honest badge, no fabricated data.
	let { data }: { data: PageData } = $props();

	// The three questions this page will answer. Titles and destinations are real; the FIGURES are
	// deliberately absent rather than mocked, and each panel names the read it is waiting on.
	const PANELS = [
		{
			title: 'Recent activity',
			icon: Activity,
			blurb: 'Runs, ingests and cascades across the estate, newest first.',
			needs: 'the lineage service’s run feed, read estate-wide rather than per-project',
		},
		{
			title: 'What landed',
			icon: Boxes,
			blurb: 'Datasets and tables that changed recently, and who changed them.',
			needs: 'a catalog read spanning every project the caller can see',
		},
		{
			title: 'Needs attention',
			icon: FolderKanban,
			blurb: 'Failed runs, dead-lettered events, and stores that stopped answering.',
			needs: 'the DLQ and health surfaces, aggregated above the zone that owns each',
		},
	];
</script>

<svelte:head><title>lance</title></svelte:head>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-6 p-6">
	<header class="flex flex-wrap items-center gap-3">
		<h1 class="text-2xl font-semibold">lance</h1>
		<Badge variant="outline">Scaffold — no insights wired yet</Badge>
	</header>
	<p class="text-muted-foreground max-w-2xl text-sm">
		The governed lakehouse estate. Open a project to work in it, or configure the estate itself — this
		page will summarise what is happening across every project you can see.
	</p>

	<!-- The two real destinations from here. SAME-ZONE links: `/projects` and `/settings` are routes
	     in this app, so they soft-navigate and must NOT carry data-sveltekit-reload. -->
	<div class="grid gap-3 sm:grid-cols-2">
		<a
			href="/projects"
			data-slot="home-projects-card"
			class="hover:bg-accent/40 focus-visible:ring-ring flex items-center gap-3 rounded-lg border p-4 transition-colors focus-visible:outline-none focus-visible:ring-2"
		>
			<FolderKanban class="size-5 shrink-0" aria-hidden="true" />
			<span class="flex flex-col">
				<span class="font-medium">Projects</span>
				<span class="text-muted-foreground text-xs">
					{data.projects.length > 0
						? `${data.projects.length} project${data.projects.length === 1 ? '' : 's'} you can open`
						: 'Browse or create a project'}
				</span>
			</span>
		</a>
		{#if data.estateAdmin}
			<a
				href="/settings"
				data-slot="home-settings-card"
				class="hover:bg-accent/40 focus-visible:ring-ring flex items-center gap-3 rounded-lg border p-4 transition-colors focus-visible:outline-none focus-visible:ring-2"
			>
				<Settings class="size-5 shrink-0" aria-hidden="true" />
				<span class="flex flex-col">
					<span class="font-medium">Settings</span>
					<span class="text-muted-foreground text-xs">Notifications, project defaults, access</span>
				</span>
			</a>
		{/if}
	</div>

	<!-- The insight panels. Each names WHAT it will show and WHICH read it waits on, so the page is a
	     plan rather than a mystery — and so nobody mistakes an empty card for a broken one. -->
	<section class="flex flex-col gap-3" aria-label="Estate insights">
		{#each PANELS as panel (panel.title)}
			<Card class="flex flex-col gap-2 p-5">
				<div class="flex items-center gap-2">
					<panel.icon class="text-muted-foreground size-4" aria-hidden="true" />
					<h2 class="font-medium">{panel.title}</h2>
					<Badge variant="outline" class="ml-auto">Not wired</Badge>
				</div>
				<p class="text-muted-foreground text-sm">{panel.blurb}</p>
				<p class="text-muted-foreground/80 text-xs">Waiting on {panel.needs}.</p>
			</Card>
		{/each}
	</section>
</div>
