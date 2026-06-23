<script lang="ts">
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import * as Sidebar from '../components/sidebar/index.js';
	import { useSidebar } from '../components/sidebar/index.js';
	import { ChevronsUpDown, Boxes, House, Check } from '@lucide/svelte';
	import type { Project } from './nav-config.js';

	// sidebar-07 TeamSwitcher, adapted into a PROJECT switcher. The dropdown swaps between
	// projects or returns to the main menu (the home picker at `/`) — that's how you leave
	// a project, which is why there's no "Home" item in the sidebar nav. It deliberately
	// does NOT create projects: that lives on the home landing, not here.
	let { project = { name: 'Default', subtitle: 'Project' } }: { project?: Project } = $props();
	const sidebar = useSidebar();

	// One implicit project until backend project support lands; the slug is the URL's
	// first segment, so swapping just navigates to /<slug>/overview.
	const projects = [{ name: 'Default', slug: 'default' }];
	const activeSlug = $derived(project.name.toLowerCase());
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
							<span class="truncate font-medium">{project.name}</span>
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
				{#each projects as p (p.slug)}
					<DropdownMenu.Item class="p-0">
						<a href="/{p.slug}/overview" class="flex w-full items-center gap-2 px-2 py-1.5">
							<div class="flex size-6 items-center justify-center rounded-md border">
								<Boxes class="size-3.5 shrink-0" />
							</div>
							{p.name}
							{#if p.slug === activeSlug}
								<Check class="ml-auto size-4" />
							{/if}
						</a>
					</DropdownMenu.Item>
				{/each}
				<DropdownMenu.Separator />
				<DropdownMenu.Item class="p-0">
					<a href="/" class="flex w-full items-center gap-2 px-2 py-1.5">
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
