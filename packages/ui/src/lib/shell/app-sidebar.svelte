<script lang="ts">
	import type { Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import { Avatar, AvatarFallback } from '../components/avatar/index.js';
	import { toggleMode } from 'mode-watcher';
	import { ChevronsUpDown, Plus, Settings, Sun, Moon, Boxes } from '@lucide/svelte';
	import { navGroups, type Project, type NavUser } from './nav-config.js';

	// Pure shared shell. The consuming app passes the current pathname (from
	// $app/state — the lib can't import $app/*) and optional project/user/status.
	// `status` is an app-specific snippet (e.g. compute renders Ray health here) so
	// the shared shell never imports app data like @rask/api.
	let {
		pathname = '',
		project = { name: 'Default', subtitle: 'Project' },
		user = { name: 'rask', email: 'local', initials: 'RA' },
		status,
	}: {
		pathname?: string;
		project?: Project;
		user?: NavUser;
		status?: Snippet;
	} = $props();
</script>

<Sidebar.Root collapsible="icon">
	<!-- Header: project switcher — the project navigator / "home" view. One implicit
	     Default project until backend project support lands. -->
	<Sidebar.Header>
		<Sidebar.Menu>
			<Sidebar.MenuItem>
				<DropdownMenu.Root>
					<DropdownMenu.Trigger>
						{#snippet child({ props })}
							<Sidebar.MenuButton
								size="lg"
								{...props}
								class="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
							>
								<div
									class="bg-sidebar-primary text-sidebar-primary-foreground flex aspect-square size-8 items-center justify-center rounded-lg"
								>
									<Boxes class="size-4" />
								</div>
								<div class="grid flex-1 text-left text-sm leading-tight">
									<span class="truncate font-semibold">{project.name}</span>
									<span class="truncate text-xs opacity-70">{project.subtitle ?? 'Project'}</span>
								</div>
								<ChevronsUpDown class="ml-auto size-4 opacity-70" />
							</Sidebar.MenuButton>
						{/snippet}
					</DropdownMenu.Trigger>
					<DropdownMenu.Content
						class="w-(--bits-dropdown-menu-anchor-width) min-w-56 rounded-lg"
						align="start"
						sideOffset={4}
					>
						<DropdownMenu.Label class="text-muted-foreground text-xs">Projects</DropdownMenu.Label>
						<DropdownMenu.Item>
							<div class="bg-sidebar-primary/10 flex size-6 items-center justify-center rounded-md">
								<Boxes class="size-3.5" />
							</div>
							{project.name}
						</DropdownMenu.Item>
						<DropdownMenu.Separator />
						<DropdownMenu.Item disabled>
							<div class="flex size-6 items-center justify-center rounded-md border">
								<Plus class="size-3.5" />
							</div>
							<span class="text-muted-foreground">New project (soon)</span>
						</DropdownMenu.Item>
					</DropdownMenu.Content>
				</DropdownMenu.Root>
			</Sidebar.MenuItem>
		</Sidebar.Menu>
	</Sidebar.Header>

	<!-- Content: grouped nav, single source of truth, base-prefixed hrefs. -->
	<Sidebar.Content>
		{#each navGroups as group (group.label)}
			<Sidebar.Group>
				<Sidebar.GroupLabel>{group.label}</Sidebar.GroupLabel>
				<Sidebar.GroupContent>
					<Sidebar.Menu>
						{#each group.items as item (item.title)}
							<Sidebar.MenuItem>
								<Sidebar.MenuButton isActive={item.match(pathname)} tooltipContent={item.title}>
									{#snippet child({ props })}
										<a href={item.href} {...props}>
											<item.icon />
											<span>{item.title}</span>
										</a>
									{/snippet}
								</Sidebar.MenuButton>
							</Sidebar.MenuItem>
						{/each}
					</Sidebar.Menu>
				</Sidebar.GroupContent>
			</Sidebar.Group>
		{/each}
	</Sidebar.Content>

	<!-- Footer: profile dropdown — identity, app status (e.g. Ray), theme, settings. -->
	<Sidebar.Footer>
		<Sidebar.Menu>
			<Sidebar.MenuItem>
				<DropdownMenu.Root>
					<DropdownMenu.Trigger>
						{#snippet child({ props })}
							<Sidebar.MenuButton
								size="lg"
								{...props}
								class="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
							>
								<Avatar class="size-8 rounded-lg">
									<AvatarFallback class="rounded-lg">{user.initials ?? 'RA'}</AvatarFallback>
								</Avatar>
								<div class="grid flex-1 text-left text-sm leading-tight">
									<span class="truncate font-semibold">{user.name}</span>
									<span class="truncate text-xs opacity-70">{user.email ?? 'local'}</span>
								</div>
								<ChevronsUpDown class="ml-auto size-4 opacity-70" />
							</Sidebar.MenuButton>
						{/snippet}
					</DropdownMenu.Trigger>
					<DropdownMenu.Content
						class="w-(--bits-dropdown-menu-anchor-width) min-w-56 rounded-lg"
						side="top"
						align="end"
						sideOffset={4}
					>
						<DropdownMenu.Label class="font-normal">
							<div class="flex items-center gap-2 py-1 text-left text-sm">
								<Avatar class="size-8 rounded-lg">
									<AvatarFallback class="rounded-lg">{user.initials ?? 'RA'}</AvatarFallback>
								</Avatar>
								<div class="grid flex-1 leading-tight">
									<span class="truncate font-semibold">{user.name}</span>
									<span class="text-muted-foreground truncate text-xs">{user.email ?? 'local'}</span
									>
								</div>
							</div>
						</DropdownMenu.Label>
						{#if status}
							<DropdownMenu.Separator />
							<div class="px-2 py-1.5">{@render status()}</div>
						{/if}
						<DropdownMenu.Separator />
						<DropdownMenu.Item onclick={toggleMode}>
							<Sun class="size-4 dark:hidden" />
							<Moon class="hidden size-4 dark:block" />
							Toggle theme
						</DropdownMenu.Item>
						<DropdownMenu.Item disabled>
							<Settings class="size-4" />
							<span class="text-muted-foreground">Settings (soon)</span>
						</DropdownMenu.Item>
					</DropdownMenu.Content>
				</DropdownMenu.Root>
			</Sidebar.MenuItem>
		</Sidebar.Menu>
	</Sidebar.Footer>

	<Sidebar.Rail />
</Sidebar.Root>
