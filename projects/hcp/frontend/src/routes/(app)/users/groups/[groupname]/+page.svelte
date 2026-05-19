<script lang="ts">
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { Button } from '$lib/components/ui/button/index.js';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import { Trash2 } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { get_group, delete_group } from '$lib/remote/users.remote.js';
	import type { GroupAccount } from '$lib/constants.js';
	import BackButton from '$lib/components/custom/back-button/back-button.svelte';
	import NoTenantPlaceholder from '$lib/components/custom/no-tenant-placeholder/no-tenant-placeholder.svelte';
	import GroupProfile from './sections/group-profile.svelte';
	import GroupNamespaceAccess from './sections/group-namespace-access.svelte';

	let tenant = $derived(page.data.tenant as string | undefined);
	let groupname = $derived(page.params.groupname ?? '');

	let groupData = $derived(tenant && groupname ? get_group({ tenant, groupname }) : undefined);
	let group = $derived((groupData?.current ?? null) as GroupAccount | null);

	// Delete
	let deleteOpen = $state(false);
	let deleting = $state(false);

	async function handleDelete() {
		if (!tenant) return;
		deleting = true;
		try {
			await delete_group({ tenant, groupname });
			toast.success(`Group "${groupname}" deleted`);
			goto('/users');
		} catch {
			toast.error('Failed to delete group');
			deleting = false;
		}
	}
</script>

<svelte:head>
	<title>{groupname} - Group Settings - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-4">
			<BackButton href="/users" label="Back to users & groups" />
			<div>
				<h2 class="text-2xl font-bold">{groupname}</h2>
				<p class="mt-1 text-sm text-muted-foreground">
					{group?.description || 'Group settings and access control'}
				</p>
			</div>
		</div>
		{#if group}
			<Button variant="destructive" size="sm" onclick={() => (deleteOpen = true)}>
				<Trash2 class="h-4 w-4" />
				Delete
			</Button>
		{/if}
	</div>

	{#if !tenant}
		<NoTenantPlaceholder message="Log in with a tenant to view group details." />
	{:else}
		<GroupProfile {tenant} {groupname} />
		<GroupNamespaceAccess {tenant} {groupname} />
	{/if}
</div>

<DeleteConfirmDialog
	bind:open={deleteOpen}
	name={groupname}
	itemType="group"
	loading={deleting}
	onconfirm={handleDelete}
/>
