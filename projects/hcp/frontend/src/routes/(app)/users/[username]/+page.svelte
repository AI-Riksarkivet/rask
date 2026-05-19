<script lang="ts">
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { Button } from '$lib/components/ui/button/index.js';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import { KeyRound, Trash2 } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { get_user, delete_user } from '$lib/remote/users.remote.js';
	import type { User } from '$lib/constants.js';
	import BackButton from '$lib/components/custom/back-button/back-button.svelte';
	import NoTenantPlaceholder from '$lib/components/custom/no-tenant-placeholder/no-tenant-placeholder.svelte';
	import UserProfile from './sections/user-profile.svelte';
	import UserS3Credentials from './sections/user-s3-credentials.svelte';
	import UserNamespaceAccess from './sections/user-namespace-access.svelte';
	import UserPasswordDialog from './sections/user-password-dialog.svelte';

	let tenant = $derived(page.data.tenant as string | undefined);
	let username = $derived(page.params.username ?? '');

	let userData = $derived(tenant && username ? get_user({ tenant, username }) : undefined);
	let user = $derived((userData?.current ?? null) as User | null);

	// Change Password
	let passwordOpen = $state(false);

	// Delete User
	let deleteOpen = $state(false);
	let deleting = $state(false);

	async function handleDelete() {
		if (!tenant) return;
		deleting = true;
		try {
			await delete_user({ tenant, username });
			toast.success(`User "${username}" deleted`);
			goto('/users');
		} catch {
			toast.error('Failed to delete user');
			deleting = false;
		}
	}
</script>

<svelte:head>
	<title>{username} - User Settings - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-4">
			<BackButton href="/users" label="Back to users" />
			<div>
				<h2 class="text-2xl font-bold">{username}</h2>
				<p class="mt-1 text-sm text-muted-foreground">
					{user?.fullName || 'User settings and access control'}
				</p>
			</div>
		</div>
		{#if user}
			<div class="flex items-center gap-2">
				<Button variant="outline" size="sm" onclick={() => (passwordOpen = true)}>
					<KeyRound class="h-4 w-4" />
					Change Password
				</Button>
				<Button variant="destructive" size="sm" onclick={() => (deleteOpen = true)}>
					<Trash2 class="h-4 w-4" />
					Delete
				</Button>
			</div>
		{/if}
	</div>

	{#if !tenant}
		<NoTenantPlaceholder message="Log in with a tenant to view user details." />
	{:else}
		<!-- Top row: General + Roles | S3 Credentials -->
		<div class="grid gap-6 xl:grid-cols-2">
			<UserProfile {tenant} {username} />
			<UserS3Credentials {tenant} {username} />
		</div>

		<!-- Namespace Access (full width) -->
		<UserNamespaceAccess {tenant} {username} />

		<UserPasswordDialog {tenant} {username} bind:open={passwordOpen} />
	{/if}
</div>

<DeleteConfirmDialog
	bind:open={deleteOpen}
	name={username}
	itemType="user"
	loading={deleting}
	onconfirm={handleDelete}
/>
