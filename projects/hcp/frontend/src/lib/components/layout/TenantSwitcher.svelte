<script lang="ts">
	import { Building2, ChevronsUpDown, Plus } from 'lucide-svelte';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import type { TenantSession } from '$lib/types/session.js';

	interface Props {
		sessions: TenantSession[];
		currentTenant?: string;
	}

	let { sessions, currentTenant }: Props = $props();

	let otherSessions = $derived(sessions.filter((s) => !s.isActive));

	async function switchTo(session: TenantSession) {
		if (session.expired) {
			const params = new URLSearchParams({ add: '' });
			if (session.tenant) params.set('tenant', session.tenant);
			window.location.href = `/login?${params}`;
			return;
		}
		const res = await fetch('/api/switch-tenant', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ cookieName: session.cookieName }),
		});
		if (res.ok) {
			window.location.href = '/namespaces';
		}
	}
</script>

<DropdownMenu.Root>
	<DropdownMenu.Trigger>
		{#snippet child({ props })}
			<Button {...props} variant="outline" size="sm" class="gap-1.5">
				<Building2 class="h-4 w-4 text-muted-foreground" />
				<span class="text-sm font-medium">{currentTenant ?? 'System'}</span>
				<ChevronsUpDown class="h-3.5 w-3.5 text-muted-foreground" />
			</Button>
		{/snippet}
	</DropdownMenu.Trigger>
	<DropdownMenu.Content align="start" class="w-64">
		<DropdownMenu.Label>Tenant Sessions</DropdownMenu.Label>
		<DropdownMenu.Separator />
		{#each sessions as session (session.cookieName)}
			<DropdownMenu.Item
				disabled={session.isActive && !session.expired}
				onclick={() => {
					if (!session.isActive) switchTo(session);
				}}
			>
				<div class="flex w-full items-center justify-between">
					<div>
						<p class="text-sm font-medium">{session.tenant ?? 'System'}</p>
						<p class="text-xs text-muted-foreground">{session.username}</p>
					</div>
					{#if session.isActive}
						<Badge variant="default" class="text-[10px]">Active</Badge>
					{:else if session.expired}
						<Badge variant="destructive" class="text-[10px]">Expired</Badge>
					{:else}
						<Badge variant="outline" class="text-[10px]">Switch</Badge>
					{/if}
				</div>
			</DropdownMenu.Item>
		{/each}
		{#if otherSessions.length > 0}
			<DropdownMenu.Separator />
		{/if}
		<DropdownMenu.Item
			onclick={() => {
				window.location.href = '/login?add';
			}}
		>
			<Plus class="h-4 w-4" />
			Add another tenant
		</DropdownMenu.Item>
	</DropdownMenu.Content>
</DropdownMenu.Root>
