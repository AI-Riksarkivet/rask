<script lang="ts">
	import { onMount } from 'svelte';
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import { Button } from '../components/button/index.js';
	import { ChevronsUpDown, Boxes, House, Check } from '@lucide/svelte';
	import type { MeProject, Project } from './nav-config.js';
	import { projectFromHost } from './breadcrumb.js';

	// sidebar-07's TeamSwitcher, adapted into a PROJECT switcher and relocated to the navbar row:
	// it is estate chrome (which project am I in), not in-zone navigation, so it sits beside the
	// sidebar HEADER, where the rail's most valuable slot used to print the zone's own name — a fact
	// the top navbar and the breadcrumb already state. Project-first
	// IA via HOST: the project IS the request host (e.g. demo.localhost), so "Main menu" returns to
	// the platform picker on the FRONT-DOOR host — derived by stripping the project's subdomain
	// label (demo.localhost -> localhost). That's how you leave a project; the picker (not this
	// switcher) lists/creates projects.
	let {
		project = { name: '', subtitle: 'Project' },
		projects = [],
	}: { project?: Project; projects?: MeProject[] } = $props();

	// Computed client-side from the current host (SSR has no window; these links are only
	// followed in the browser). homeUrl = platform front-door (host minus the project
	// label); currentSlug = the active project (first host label).
	let homeUrl = $state('/');
	let currentSlug = $state('');
	onMount(() => {
		const { protocol, host } = window.location;
		// Shared `projectFromHost` (single source of truth) decides whether the host
		// carries a project label — bare/IPv4 hosts don't; keeps this in lockstep with
		// the app-shell breadcrumb root.
		const slug = projectFromHost(host);
		if (slug) {
			currentSlug = slug;
			homeUrl = `${protocol}//${host.split('.').slice(1).join('.')}/`;
		}
	});
	// The active project (host slug wins, else the cookie-fed prop). Empty = none active — say
	// so, never 'Default' (#103).
	const activeSlug = $derived(currentSlug || project.name);
	const displayName = $derived(activeSlug || 'Select project');
</script>

<DropdownMenu.Root>
	<DropdownMenu.Trigger>
		<!-- Rendered as the SIDEBAR HEADER, so it keeps that shape: the accented icon block and the
		     two-line label are what made the header read as a header rather than a control that happened
		     to sit at the top. A plain ghost button lost both — a bare glyph, one truncated line, and no
		     accent — which is why the rail suddenly looked like it was missing something.
		     `size-8` block + `text-sm leading-tight` stack matches the sidebar-07 header the estate
		     already uses; collapsed to icon only the block survives, which is the intended silhouette.

		     ICON MODE IS CLAMPED (#111). This is a plain ghost Button, not a `Sidebar.MenuButton`, so it
		     never inherited that primitive's `group-data-[collapsible=icon]:size-8!/p-2!` — and without
		     it the 32px `shrink-0` block sat inside a 31px button in a 48px rail, hanging half a pixel
		     past its own edge while every nav icon below lined up. Measured, not guessed: rail 48,
		     button 31, block 32. The `!` matters — Button's own size classes would otherwise win. -->
		{#snippet child({ props })}
			<Button
				{...props}
				variant="ghost"
				class="text-foreground h-auto w-full min-w-0 justify-start gap-2 px-2 py-1.5 group-data-[collapsible=icon]:size-8! group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:p-0!"
				aria-label="Switch project"
			>
				<div
					class="bg-sidebar-primary text-sidebar-primary-foreground flex aspect-square size-8 shrink-0 items-center justify-center rounded-lg"
				>
					<Boxes class="size-4" />
				</div>
				<div
					class="grid min-w-0 flex-1 text-left text-sm leading-tight group-data-[collapsible=icon]:hidden"
				>
					<span class="truncate font-medium capitalize">{displayName}</span>
					<span class="text-muted-foreground truncate text-xs">{project.subtitle ?? 'Project'}</span>
				</div>
				<ChevronsUpDown
					class="text-muted-foreground size-4 shrink-0 group-data-[collapsible=icon]:hidden"
				/>
			</Button>
		{/snippet}
	</DropdownMenu.Trigger>
	<DropdownMenu.Content class="min-w-56 rounded-lg" align="start" side="bottom" sideOffset={4}>
		<DropdownMenu.Label class="text-muted-foreground text-xs">Projects</DropdownMenu.Label>
		<!-- YOUR projects, from /v1/me — the memberships every zone already holds. Picking one opens
		     its overview (/projects/<id> stamps the active-project cookie, #103). The active one is
		     checked. An empty list degrades to one Browse row rather than a dropdown that says
		     "select" while offering nothing to select. -->
		{#each projects as m (m.project)}
			<DropdownMenu.Item class="p-0">
				<a
					href={`/projects/${m.project}`}
					data-sveltekit-reload
					class="flex w-full items-center gap-2 px-2 py-1.5"
				>
					<div class="flex size-6 items-center justify-center rounded-md border">
						<Boxes class="size-3.5 shrink-0" />
					</div>
					<span class="truncate capitalize">{m.project}</span>
					{#if m.project === activeSlug}<Check class="ml-auto size-4" />{/if}
				</a>
			</DropdownMenu.Item>
		{/each}
		<DropdownMenu.Item class="p-0">
			<a href="/projects" data-sveltekit-reload class="flex w-full items-center gap-2 px-2 py-1.5">
				<div class="flex size-6 items-center justify-center rounded-md border">
					<Boxes class="size-3.5 shrink-0" />
				</div>
				<span class="text-muted-foreground truncate">Browse all projects</span>
			</a>
		</DropdownMenu.Item>
		<DropdownMenu.Separator />
		<DropdownMenu.Item class="p-0">
			<!-- data-sveltekit-reload: cross-zone by definition (home owns '/'), so a soft nav would ask
			     THIS zone for a route it does not own and 404. Same rule every cross-zone link follows. -->
			<a href={homeUrl} data-sveltekit-reload class="flex w-full items-center gap-2 px-2 py-1.5">
				<div class="flex size-6 items-center justify-center rounded-md border bg-transparent">
					<House class="size-4" />
				</div>
				<span class="text-muted-foreground font-medium">Main menu</span>
			</a>
		</DropdownMenu.Item>
	</DropdownMenu.Content>
</DropdownMenu.Root>
