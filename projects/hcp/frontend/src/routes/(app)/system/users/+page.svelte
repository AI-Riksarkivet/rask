<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Table from '$lib/components/ui/table/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import FormDialog from '$lib/components/custom/form-dialog/form-dialog.svelte';
	import { toast } from 'svelte-sonner';
	import {
		get_system_users,
		change_system_user_password,
		type SystemUser,
	} from '$lib/remote/system.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let usersData = $derived(get_system_users({}));
	let users = $derived((usersData?.current ?? []) as SystemUser[]);

	let changePasswordOpen = $state(false);
	let selectedUser = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let passwordError = $state('');
	let passwordLoading = $state(false);

	function openChangePassword(username: string) {
		selectedUser = username;
		newPassword = '';
		confirmPassword = '';
		passwordError = '';
		changePasswordOpen = true;
	}

	async function handleChangePassword(e: SubmitEvent) {
		e.preventDefault();
		if (newPassword !== confirmPassword) {
			passwordError = 'Passwords do not match';
			return;
		}
		if (newPassword.length < 6) {
			passwordError = 'Password must be at least 6 characters';
			return;
		}
		passwordLoading = true;
		passwordError = '';
		try {
			await change_system_user_password({
				username: selectedUser,
				newPassword,
			});
			toast.success(`Password changed for ${selectedUser}`);
			changePasswordOpen = false;
		} catch (err) {
			passwordError = getErrorMessage(err, 'Failed to change password');
		} finally {
			passwordLoading = false;
		}
	}
</script>

<svelte:head>
	<title>System Users - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="System User Accounts"
		description="Manage system-level user accounts and credentials"
	/>

	{#await usersData}
		<CardSkeleton />
	{:then}
		<Card.Root>
			<Card.Header>
				<Card.Title>User Accounts</Card.Title>
				<Card.Description>
					{users.length} system user{users.length !== 1 ? 's' : ''}
				</Card.Description>
			</Card.Header>
			<Card.Content>
				{#if users.length === 0}
					<p class="py-8 text-center text-sm text-muted-foreground">No system users found.</p>
				{:else}
					<Table.Root>
						<Table.Header>
							<Table.Row>
								<Table.Head>Username</Table.Head>
								<Table.Head>Full Name</Table.Head>
								<Table.Head>Description</Table.Head>
								<Table.Head>Roles</Table.Head>
								<Table.Head>Status</Table.Head>
								<Table.Head>Auth</Table.Head>
								<Table.Head class="w-[100px]">Actions</Table.Head>
							</Table.Row>
						</Table.Header>
						<Table.Body>
							{#each users as user (user.username)}
								<Table.Row>
									<Table.Cell class="font-medium">{user.username ?? '—'}</Table.Cell>
									<Table.Cell>{user.fullName ?? '—'}</Table.Cell>
									<Table.Cell class="max-w-[200px] truncate text-muted-foreground">
										{user.description ?? '—'}
									</Table.Cell>
									<Table.Cell>
										<div class="flex flex-wrap gap-1">
											{#each user.roles?.role ?? [] as role (role)}
												<Badge variant="outline" class="text-xs">{role}</Badge>
											{/each}
										</div>
									</Table.Cell>
									<Table.Cell>
										{#if user.enabled}
											<Badge variant="default">Enabled</Badge>
										{:else}
											<Badge variant="destructive">Disabled</Badge>
										{/if}
									</Table.Cell>
									<Table.Cell>
										{#if user.localAuthentication}
											<Badge variant="secondary">Local</Badge>
										{:else}
											<Badge variant="outline">External</Badge>
										{/if}
									</Table.Cell>
									<Table.Cell>
										<Button
											variant="ghost"
											size="sm"
											onclick={() => openChangePassword(user.username ?? '')}
										>
											Password
										</Button>
									</Table.Cell>
								</Table.Row>
							{/each}
						</Table.Body>
					</Table.Root>
				{/if}
			</Card.Content>
		</Card.Root>
	{/await}
</div>

<FormDialog
	bind:open={changePasswordOpen}
	title="Change Password — {selectedUser}"
	loading={passwordLoading}
	error={passwordError}
	onsubmit={handleChangePassword}
>
	<div class="space-y-4">
		<div class="space-y-1.5">
			<Label for="new-password">New Password</Label>
			<Input id="new-password" type="password" bind:value={newPassword} required />
		</div>
		<div class="space-y-1.5">
			<Label for="confirm-password">Confirm Password</Label>
			<Input id="confirm-password" type="password" bind:value={confirmPassword} required />
		</div>
	</div>
</FormDialog>
