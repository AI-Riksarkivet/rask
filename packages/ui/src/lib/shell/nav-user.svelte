<script lang="ts">
	import type { Snippet } from 'svelte';
	import { Avatar, AvatarFallback } from '../components/avatar/index.js';
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import * as Sidebar from '../components/sidebar/index.js';
	import { useSidebar } from '../components/sidebar/index.js';
	import { toggleMode } from 'mode-watcher';
	import { ChevronsUpDown, Sun, Moon, Settings } from '@lucide/svelte';
	import type { NavUser } from './nav-config.js';

	// sidebar-07 NavUser, adapted: identity + an app-specific `status` slot (e.g.
	// compute renders Ray health) + theme toggle + settings. The shared shell never
	// imports app data like @rask/api — the app passes `status` in.
	let {
		user = { name: 'rask', email: 'local', initials: 'RA' },
		status,
	}: { user?: NavUser; status?: Snippet } = $props();
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
						<Avatar class="size-8 rounded-lg">
							<AvatarFallback class="rounded-lg">{user.initials ?? 'RA'}</AvatarFallback>
						</Avatar>
						<div class="grid flex-1 text-left text-sm leading-tight">
							<span class="truncate font-medium">{user.name}</span>
							<span class="truncate text-xs">{user.email ?? 'local'}</span>
						</div>
						<ChevronsUpDown class="ml-auto size-4" />
					</Sidebar.MenuButton>
				{/snippet}
			</DropdownMenu.Trigger>
			<DropdownMenu.Content
				class="w-(--bits-dropdown-menu-anchor-width) min-w-56 rounded-lg"
				side={sidebar.isMobile ? 'bottom' : 'right'}
				align="end"
				sideOffset={4}
			>
				<DropdownMenu.Label class="p-0 font-normal">
					<div class="flex items-center gap-2 px-1 py-1.5 text-left text-sm">
						<Avatar class="size-8 rounded-lg">
							<AvatarFallback class="rounded-lg">{user.initials ?? 'RA'}</AvatarFallback>
						</Avatar>
						<div class="grid flex-1 text-left text-sm leading-tight">
							<span class="truncate font-medium">{user.name}</span>
							<span class="truncate text-xs">{user.email ?? 'local'}</span>
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
					Settings (soon)
				</DropdownMenu.Item>
			</DropdownMenu.Content>
		</DropdownMenu.Root>
	</Sidebar.MenuItem>
</Sidebar.Menu>
