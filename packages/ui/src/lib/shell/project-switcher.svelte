<script lang="ts">
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import * as Sidebar from '../components/sidebar/index.js';
	import { useSidebar } from '../components/sidebar/index.js';
	import { ChevronsUpDown, Plus, Boxes } from '@lucide/svelte';
	import type { Project } from './nav-config.js';

	// sidebar-07 TeamSwitcher, adapted: a project switcher / "home" navigator. One
	// implicit Default project until backend project support lands.
	let { project = { name: 'Default', subtitle: 'Project' } }: { project?: Project } = $props();
	const sidebar = useSidebar();
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
				<DropdownMenu.Item class="gap-2 p-2">
					<div class="flex size-6 items-center justify-center rounded-md border">
						<Boxes class="size-3.5 shrink-0" />
					</div>
					{project.name}
				</DropdownMenu.Item>
				<DropdownMenu.Separator />
				<DropdownMenu.Item disabled class="gap-2 p-2">
					<div class="flex size-6 items-center justify-center rounded-md border bg-transparent">
						<Plus class="size-4" />
					</div>
					<div class="text-muted-foreground font-medium">New project (soon)</div>
				</DropdownMenu.Item>
			</DropdownMenu.Content>
		</DropdownMenu.Root>
	</Sidebar.MenuItem>
</Sidebar.Menu>
