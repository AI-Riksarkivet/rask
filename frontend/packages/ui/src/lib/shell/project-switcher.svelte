<script lang="ts">
	import { onMount } from 'svelte';
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import { Button } from '../components/button/index.js';
	import { ChevronsUpDown, Boxes, House, Check } from '@lucide/svelte';
	import type { Project } from './nav-config.js';
	import { projectFromHost } from './breadcrumb.js';

	// sidebar-07's TeamSwitcher, adapted into a PROJECT switcher and relocated to the navbar row:
	// it is estate chrome (which project am I in), not in-zone navigation, so it sits beside the
	// sidebar trigger at the head of the shell header rather than inside the sidebar. Project-first
	// IA via HOST: the project IS the request host (e.g. demo.localhost), so "Main menu" returns to
	// the platform picker on the FRONT-DOOR host — derived by stripping the project's subdomain
	// label (demo.localhost -> localhost). That's how you leave a project; the picker (not this
	// switcher) lists/creates projects.
	let { project = { name: 'Default', subtitle: 'Project' } }: { project?: Project } = $props();

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
	const displayName = $derived(currentSlug || project.name);
</script>

<DropdownMenu.Root>
	<DropdownMenu.Trigger>
		{#snippet child({ props })}
			<Button
				{...props}
				variant="ghost"
				class="text-foreground max-w-48 min-w-0 gap-1.5 px-2"
				aria-label="Switch project"
			>
				<Boxes class="text-muted-foreground" />
				<span class="truncate capitalize">{displayName}</span>
				<ChevronsUpDown class="text-muted-foreground" />
			</Button>
		{/snippet}
	</DropdownMenu.Trigger>
	<DropdownMenu.Content class="min-w-56 rounded-lg" align="start" side="bottom" sideOffset={4}>
		<DropdownMenu.Label class="text-muted-foreground text-xs">Projects</DropdownMenu.Label>
		<DropdownMenu.Item class="p-0">
			<a
				href="/lakehouse/data"
				data-sveltekit-reload
				class="flex w-full items-center gap-2 px-2 py-1.5"
			>
				<div class="flex size-6 items-center justify-center rounded-md border">
					<Boxes class="size-3.5 shrink-0" />
				</div>
				<span class="truncate capitalize">{displayName}</span>
				<Check class="ml-auto size-4" />
			</a>
		</DropdownMenu.Item>
		<DropdownMenu.Separator />
		<DropdownMenu.Item class="p-0">
			<a href={homeUrl} class="flex w-full items-center gap-2 px-2 py-1.5">
				<div class="flex size-6 items-center justify-center rounded-md border bg-transparent">
					<House class="size-4" />
				</div>
				<span class="text-muted-foreground font-medium">Main menu</span>
			</a>
		</DropdownMenu.Item>
	</DropdownMenu.Content>
</DropdownMenu.Root>
