<script lang="ts">
	import { resolve } from '$app/paths';
	import { FolderKanban, Users } from '@lucide/svelte';
	import { Subject } from '#lib/access/identity/index.ts';
	import type { PageData } from './$types';

	// SETTINGS — the PLATFORM level: what configures the whole installation, as opposed to what any one
	// project or page does. Estate-admin only: the gate is in `+layout.ts` (it covers this page AND its
	// children), because hiding the sidebar entry is presentation, not authorization.
	//
	// Every row is a page that works. Each reads across every project and takes none, so none belongs
	// inside one: who may do what (the FGA explorer over the whole tuple store) and the projects
	// themselves. The lakehouse keeps its PER-OBJECT grants on each object's own Access tab, which is a
	// different question at a different level.
	let { data }: { data: PageData } = $props();

	/** Every lucide icon shares one component signature, so any icon's type fits. */
	type IconComponent = typeof Users;

	const PLATFORM_ROW =
		'hover:bg-accent/40 focus-visible:ring-ring flex items-center gap-3 rounded-lg border p-4 transition-colors focus-visible:outline-none focus-visible:ring-2';
</script>

{#snippet platformRow(Icon: IconComponent, title: string, blurb: string)}
	<Icon class="size-5 shrink-0" aria-hidden="true" />
	<span class="flex min-w-0 flex-col">
		<span class="font-medium">{title}</span>
		<span class="text-muted-foreground text-xs">{blurb}</span>
	</span>
{/snippet}

<svelte:head><title>Settings · rask</title></svelte:head>

<div class="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">Settings</h1>
		<p class="text-muted-foreground text-sm">
			Platform-wide configuration. Signed in as an estate admin — everything here affects every
			project.
		</p>
	</header>

	<section class="flex flex-col gap-2" aria-labelledby="settings-authz">
		<h2
			id="settings-authz"
			class="text-muted-foreground text-xs font-semibold uppercase tracking-wide"
		>
			Access &amp; authorization
		</h2>
		<p class="text-muted-foreground text-xs">
			Each reads across every project; none is scoped to one.
		</p>
		<a href={resolve('/settings/access')} class={PLATFORM_ROW}>
			{@render platformRow(
				Users,
				'Users & roles',
				'Who may do what, platform-wide — the FGA explorer over the whole tuple store.',
			)}
		</a>
		<a href={resolve('/projects')} class={PLATFORM_ROW}>
			{@render platformRow(
				FolderKanban,
				'Projects',
				'Every project the platform knows, its warehouses and their effective admins.',
			)}
		</a>
	</section>

	<p class="text-muted-foreground/80 text-xs" data-testid="signed-in-as">
		Signed in as {#if data.meName}<span class="text-foreground font-medium">{data.meName}</span>
		{/if}<Subject value={data.meSubject} />.
	</p>
</div>
