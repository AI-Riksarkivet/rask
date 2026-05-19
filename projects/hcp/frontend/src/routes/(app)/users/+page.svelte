<script lang="ts">
	import { page } from '$app/state';
	import { Users, UsersRound } from 'lucide-svelte';
	import * as Tabs from '$lib/components/ui/tabs/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { get_users, get_groups } from '$lib/remote/users.remote.js';
	import type { User, GroupAccount } from '$lib/constants.js';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import NoTenantPlaceholder from '$lib/components/custom/no-tenant-placeholder/no-tenant-placeholder.svelte';
	import UsersTable from './sections/users-table.svelte';
	import GroupsTable from './sections/groups-table.svelte';
	import UserCreateDialog from './sections/user-create-dialog.svelte';
	import GroupCreateDialog from './sections/group-create-dialog.svelte';

	let tenant = $derived(page.data.tenant as string | undefined);
	let usersData = $derived(tenant ? get_users({ tenant }) : undefined);
	let groupsData = $derived(tenant ? get_groups({ tenant }) : undefined);
	let users = $derived((usersData?.current ?? []) as User[]);
	let groups = $derived((groupsData?.current ?? []) as GroupAccount[]);

	let activeTab = $state('users');
	let createUserOpen = $state(false);
	let createGroupOpen = $state(false);
</script>

<svelte:head>
	<title>Users & Groups - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader title="Users & Groups" description="Manage user accounts and groups" />

	{#if tenant}
		<Tabs.Root bind:value={activeTab}>
			<Tabs.List>
				<Tabs.Trigger value="users">
					<Users class="mr-1.5 h-4 w-4" />
					Users
					{#if users.length > 0}
						<Badge variant="secondary" class="ml-1.5">{users.length}</Badge>
					{/if}
				</Tabs.Trigger>
				<Tabs.Trigger value="groups">
					<UsersRound class="mr-1.5 h-4 w-4" />
					Groups
					{#if groups.length > 0}
						<Badge variant="secondary" class="ml-1.5">{groups.length}</Badge>
					{/if}
				</Tabs.Trigger>
			</Tabs.List>

			<Tabs.Content value="users" class="space-y-4">
				<UsersTable {tenant} oncreate={() => (createUserOpen = true)} />
			</Tabs.Content>

			<Tabs.Content value="groups" class="space-y-4">
				<GroupsTable {tenant} oncreate={() => (createGroupOpen = true)} />
			</Tabs.Content>
		</Tabs.Root>

		<UserCreateDialog {tenant} bind:open={createUserOpen} />
		<GroupCreateDialog {tenant} bind:open={createGroupOpen} />
	{:else}
		<NoTenantPlaceholder message="Log in with a tenant to manage users and groups." />
	{/if}
</div>
