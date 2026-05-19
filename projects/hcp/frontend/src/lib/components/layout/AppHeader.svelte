<script lang="ts">
	import { LogOut, Moon, Sun, User, Copy, Check, Settings } from 'lucide-svelte';
	import { toggleMode, mode } from 'mode-watcher';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Separator } from '$lib/components/ui/separator/index.js';
	import { toast } from 'svelte-sonner';
	import { useCopyFeedback } from '$lib/utils/use-copy-feedback.svelte.js';
	import TenantSwitcher from './TenantSwitcher.svelte';
	import type { TenantSession } from '$lib/types/session.js';

	interface Props {
		username: string;
		tenant?: string;
		userGUID?: string;
		sessions?: TenantSession[];
	}

	let { username, tenant, userGUID, sessions = [] }: Props = $props();

	const { copied, copy } = useCopyFeedback();

	async function copyCanonicalId() {
		if (!userGUID) return;
		await copy(userGUID);
		toast.success('Canonical ID copied');
	}
</script>

<header
	class="flex h-16 shrink-0 items-center gap-2 border-b px-4 transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-12"
>
	<div class="flex flex-1 items-center gap-2">
		<Sidebar.Trigger />
		<Separator orientation="vertical" class="mx-2 data-[orientation=vertical]:h-4" />
		{#if sessions.length > 0}
			<TenantSwitcher {sessions} currentTenant={tenant} />
		{:else}
			<h1 class="text-lg font-semibold">
				{#if tenant}
					Tenant: {tenant}
				{:else}
					RA-HCP Admin Console
				{/if}
			</h1>
		{/if}
	</div>

	<DropdownMenu.Root>
		<DropdownMenu.Trigger>
			{#snippet child({ props })}
				<Button {...props} variant="outline" size="sm" class="gap-2">
					<div
						class="flex h-6 w-6 items-center justify-center rounded-full bg-primary/10 text-primary"
					>
						<User class="h-3.5 w-3.5" />
					</div>
					<span class="text-sm font-medium">{username}</span>
				</Button>
			{/snippet}
		</DropdownMenu.Trigger>
		<DropdownMenu.Content align="end" class="w-64">
			<div class="px-3 py-2.5">
				<div class="flex items-center gap-3">
					<div
						class="flex h-9 w-9 items-center justify-center rounded-full bg-primary/10 text-primary"
					>
						<User class="h-4 w-4" />
					</div>
					<div class="flex-1 overflow-hidden">
						<p class="text-sm font-semibold">{username}</p>
						{#if tenant}
							<p class="truncate text-xs text-muted-foreground">{tenant}</p>
						{/if}
					</div>
				</div>
				{#if userGUID}
					<div class="mt-2.5 space-y-1">
						<p class="text-[11px] font-medium text-muted-foreground">Canonical ID</p>
						<button
							class="flex w-full cursor-pointer items-center gap-1.5 rounded-md bg-muted/60 px-2.5 py-1.5 transition-colors hover:bg-muted"
							onclick={copyCanonicalId}
							title="Click to copy canonical ID"
						>
							<span class="flex-1 truncate text-left font-mono text-[11px] text-muted-foreground">
								{userGUID}
							</span>
							{#if copied}
								<Check class="h-3 w-3 shrink-0 text-emerald-500" />
							{:else}
								<Copy class="h-3 w-3 shrink-0 text-muted-foreground" />
							{/if}
						</button>
					</div>
				{/if}
			</div>
			<DropdownMenu.Separator />
			<DropdownMenu.Item
				onclick={() => {
					window.location.href = '/settings';
				}}
			>
				<Settings class="h-4 w-4" />
				Settings
			</DropdownMenu.Item>
			<DropdownMenu.Item onclick={toggleMode}>
				{#if mode.current === 'dark'}
					<Sun class="h-4 w-4" />
					Light mode
				{:else}
					<Moon class="h-4 w-4" />
					Dark mode
				{/if}
			</DropdownMenu.Item>
			<DropdownMenu.Separator />
			{#if sessions.length > 1}
				<DropdownMenu.Item
					variant="destructive"
					onclick={() => {
						window.location.href = '/logout';
					}}
				>
					<LogOut class="h-4 w-4" />
					Logout from {tenant ?? 'System'}
				</DropdownMenu.Item>
				<DropdownMenu.Item
					variant="destructive"
					onclick={() => {
						window.location.href = '/logout?all=true';
					}}
				>
					<LogOut class="h-4 w-4" />
					Logout from all tenants
				</DropdownMenu.Item>
			{:else}
				<DropdownMenu.Item
					variant="destructive"
					onclick={() => {
						window.location.href = '/logout';
					}}
				>
					<LogOut class="h-4 w-4" />
					Logout
				</DropdownMenu.Item>
			{/if}
		</DropdownMenu.Content>
	</DropdownMenu.Root>
</header>
