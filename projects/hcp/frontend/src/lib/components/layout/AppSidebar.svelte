<script lang="ts">
	import { page } from '$app/state';
	import {
		Database,
		Table2,
		Users,
		Server,
		Boxes,
		Settings,
		Search,
		Shield,
		FileType,
		Network,
		KeyRound,
		HardDrive,
		Activity,
		FileDown,
		HeartPulse,
		Building2,
		Headset,
		RefreshCw,
		Layers,
	} from 'lucide-svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import { APP_VERSION, type AccessLevel } from '$lib/constants.js';

	let { accessLevel }: { accessLevel: AccessLevel } = $props();

	const isAdmin = $derived(accessLevel !== 'namespace-user');
	const isSysAdmin = $derived(accessLevel === 'sys-admin');

	const tenantItems = [
		{ href: '/namespaces', label: 'Namespaces', icon: Boxes },
		{ href: '/users', label: 'Users & Groups', icon: Users },
		{ href: '/tenant-settings', label: 'Tenant Settings', icon: Settings },
	] as const;

	const searchItems = [
		{ href: '/search', label: 'Search', icon: Search },
		{ href: '/content-classes', label: 'Content Classes', icon: FileType },
	] as const;

	const storageItems = [
		{ href: '/buckets', label: 'Buckets', icon: Database },
		{ href: '/access-control', label: 'Access Control', icon: Shield },
	] as const;

	const analyticsItems = [{ href: '/analytics', label: 'Data Explorer', icon: Table2 }] as const;

	const systemItems = [
		{ href: '/system/network', label: 'Network', icon: Network },
		{ href: '/system/licenses', label: 'Licenses', icon: KeyRound },
		{ href: '/system/nodes', label: 'Nodes', icon: HardDrive },
		{ href: '/system/services', label: 'Services', icon: Activity },
		{ href: '/system/logs', label: 'Logs', icon: FileDown },
		{ href: '/system/health', label: 'Health', icon: HeartPulse },
		{ href: '/system/tenants', label: 'Tenants', icon: Building2 },
		{ href: '/system/users', label: 'Users', icon: Users },
		{ href: '/system/groups', label: 'Groups', icon: Shield },
		{ href: '/system/replication', label: 'Replication', icon: RefreshCw },
		{ href: '/system/erasure-coding', label: 'Erasure Coding', icon: Layers },
		{ href: '/system/support', label: 'Support', icon: Headset },
	] as const;
</script>

<Sidebar.Root collapsible="icon">
	<Sidebar.Header>
		<Sidebar.Menu>
			<Sidebar.MenuItem>
				<Sidebar.MenuButton size="lg">
					<div class="flex items-center gap-3 overflow-hidden">
						<Server class="h-6 w-6 shrink-0 text-primary" />
						<div class="flex flex-col leading-tight">
							<span class="whitespace-nowrap text-lg font-bold">RA-HCP</span>
							<span class="text-[10px] text-muted-foreground">v{APP_VERSION}</span>
						</div>
					</div>
				</Sidebar.MenuButton>
			</Sidebar.MenuItem>
		</Sidebar.Menu>
	</Sidebar.Header>

	<Sidebar.Content>
		{#if isAdmin}
			<Sidebar.Group>
				<Sidebar.GroupLabel>Tenant</Sidebar.GroupLabel>
				<Sidebar.GroupContent>
					<Sidebar.Menu>
						{#each tenantItems as item (item.href)}
							{@const active = page.url.pathname.startsWith(item.href)}
							<Sidebar.MenuItem>
								<Sidebar.MenuButton isActive={active} tooltipContent={item.label}>
									{#snippet child({ props })}
										<a href={item.href} {...props}>
											<item.icon class="h-5 w-5 shrink-0" />
											<span>{item.label}</span>
										</a>
									{/snippet}
								</Sidebar.MenuButton>
							</Sidebar.MenuItem>
						{/each}
					</Sidebar.Menu>
				</Sidebar.GroupContent>
			</Sidebar.Group>

			<Sidebar.Group>
				<Sidebar.GroupLabel>Search & Indexing</Sidebar.GroupLabel>
				<Sidebar.GroupContent>
					<Sidebar.Menu>
						{#each searchItems as item (item.href)}
							{@const active = page.url.pathname.startsWith(item.href)}
							<Sidebar.MenuItem>
								<Sidebar.MenuButton isActive={active} tooltipContent={item.label}>
									{#snippet child({ props })}
										<a href={item.href} {...props}>
											<item.icon class="h-5 w-5 shrink-0" />
											<span>{item.label}</span>
										</a>
									{/snippet}
								</Sidebar.MenuButton>
							</Sidebar.MenuItem>
						{/each}
					</Sidebar.Menu>
				</Sidebar.GroupContent>
			</Sidebar.Group>
		{/if}

		<Sidebar.Group>
			<Sidebar.GroupLabel>Storage</Sidebar.GroupLabel>
			<Sidebar.GroupContent>
				<Sidebar.Menu>
					{#each storageItems as item (item.href)}
						{@const active = page.url.pathname.startsWith(item.href)}
						<Sidebar.MenuItem>
							<Sidebar.MenuButton isActive={active} tooltipContent={item.label}>
								{#snippet child({ props })}
									<a href={item.href} {...props}>
										<item.icon class="h-5 w-5 shrink-0" />
										<span>{item.label}</span>
									</a>
								{/snippet}
							</Sidebar.MenuButton>
						</Sidebar.MenuItem>
					{/each}
				</Sidebar.Menu>
			</Sidebar.GroupContent>
		</Sidebar.Group>

		<Sidebar.Group>
			<Sidebar.GroupLabel>Analytics</Sidebar.GroupLabel>
			<Sidebar.GroupContent>
				<Sidebar.Menu>
					{#each analyticsItems as item (item.href)}
						{@const active = page.url.pathname.startsWith(item.href)}
						<Sidebar.MenuItem>
							<Sidebar.MenuButton isActive={active} tooltipContent={item.label}>
								{#snippet child({ props })}
									<a href={item.href} {...props}>
										<item.icon class="h-5 w-5 shrink-0" />
										<span>{item.label}</span>
									</a>
								{/snippet}
							</Sidebar.MenuButton>
						</Sidebar.MenuItem>
					{/each}
				</Sidebar.Menu>
			</Sidebar.GroupContent>
		</Sidebar.Group>

		{#if isSysAdmin}
			<Sidebar.Group>
				<Sidebar.GroupLabel>System</Sidebar.GroupLabel>
				<Sidebar.GroupContent>
					<Sidebar.Menu>
						{#each systemItems as item (item.href)}
							{@const active = page.url.pathname.startsWith(item.href)}
							<Sidebar.MenuItem>
								<Sidebar.MenuButton isActive={active} tooltipContent={item.label}>
									{#snippet child({ props })}
										<a href={item.href} {...props}>
											<item.icon class="h-5 w-5 shrink-0" />
											<span>{item.label}</span>
										</a>
									{/snippet}
								</Sidebar.MenuButton>
							</Sidebar.MenuItem>
						{/each}
					</Sidebar.Menu>
				</Sidebar.GroupContent>
			</Sidebar.Group>
		{/if}
	</Sidebar.Content>
</Sidebar.Root>
