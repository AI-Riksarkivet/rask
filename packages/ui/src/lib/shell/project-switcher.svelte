<script lang="ts">
	import { onMount } from 'svelte';
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import * as Sidebar from '../components/sidebar/index.js';
	import { useSidebar } from '../components/sidebar/index.js';
	import { ChevronsUpDown, Boxes, House, Check } from '@lucide/svelte';
	import type { Project } from './nav-config.js';
	import { projectFromHost } from './breadcrumb.js';

	// sidebar-07 TeamSwitcher, adapted into a PROJECT switcher. Project-first IA via HOST:
	// the project IS the request host (e.g. demo.localhost), so "Main menu" returns to the
	// platform picker on the FRONT-DOOR host — derived by stripping the project's subdomain
	// label (demo.localhost -> localhost). That's how you leave a project; the picker (not
	// this switcher) lists/creates projects.
	let { project = { name: 'Default', subtitle: 'Project' } }: { project?: Project } = $props();
	const sidebar = useSidebar();

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

<Sidebar.Menu>
	<Sidebar.MenuItem>
		<DropdownMenu.Root>
			<DropdownMenu.Trigger>
				{#snippet child({ props })}
					<Sidebar.MenuButton
						{...props}
						size="lg"
						class="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
					>
						<div
							class="bg-sidebar-primary text-sidebar-primary-foreground flex aspect-square size-8 items-center justify-center rounded-lg"
						>
							<Boxes class="size-4" />
						</div>
						<div class="grid flex-1 text-left text-sm leading-tight">
							<span class="truncate font-medium">{displayName}</span>
							<span class="truncate text-xs">{project.subtitle ?? 'Project'}</span>
						</div>
						<ChevronsUpDown class="ml-auto" />
					</Sidebar.MenuButton>
				{/snippet}
			</DropdownMenu.Trigger>
			<DropdownMenu.Content
				class="w-(--bits-dropdown-menu-anchor-width) min-w-56 rounded-lg"
				align="start"
				side={sidebar.isMobile ? 'bottom' : 'right'}
				sideOffset={4}
			>
				<DropdownMenu.Label class="text-muted-foreground text-xs">Projects</DropdownMenu.Label>
				<DropdownMenu.Item class="p-0">
					<a href="/overview" class="flex w-full items-center gap-2 px-2 py-1.5">
						<div class="flex size-6 items-center justify-center rounded-md border">
							<Boxes class="size-3.5 shrink-0" />
						</div>
						{displayName}
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
	</Sidebar.MenuItem>
</Sidebar.Menu>
