<script lang="ts">
	import { Plus, Search, Copy } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { toast } from 'svelte-sonner';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import {
		get_users,
		get_user_permissions,
		type DataAccessPermissions,
	} from '$lib/remote/users.remote.js';
	import { type User, getUserRoles } from '$lib/constants.js';
	import {
		DataTable,
		DataTableHeaderButton,
		createSvelteTable,
		getCoreRowModel,
		getSortedRowModel,
		getPaginationRowModel,
		renderSnippet,
		renderComponent,
	} from '$lib/components/ui/data-table/index.js';
	import type { ColumnDef, SortingState, PaginationState } from '@tanstack/table-core';
	import { SvelteMap } from 'svelte/reactivity';

	let {
		tenant,
		oncreate,
	}: {
		tenant: string;
		oncreate: () => void;
	} = $props();

	let usersData = $derived(get_users({ tenant }));
	let users = $derived((usersData?.current ?? []) as User[]);

	// Namespace access per user (reactive queries)
	let userPermsMap = $derived.by(() => {
		const map = new SvelteMap<string, ReturnType<typeof get_user_permissions>>();
		for (const u of users) {
			map.set(u.username, get_user_permissions({ tenant, username: u.username }));
		}
		return map;
	});

	function getUserNamespaces(username: string): string[] | undefined {
		const query = userPermsMap.get(username);
		if (!query?.current) return undefined;
		const perms = query.current as DataAccessPermissions;
		return (perms.namespacePermission ?? [])
			.filter((entry) => entry.permissions?.permission && entry.permissions.permission.length > 0)
			.map((entry) => entry.namespaceName);
	}

	// Search + filtered users
	let userSearch = $state('');
	let filteredUsers = $derived(
		users.filter((u) => {
			const q = userSearch.toLowerCase();
			if (!q) return true;
			return u.username.toLowerCase().includes(q) || (u.fullName ?? '').toLowerCase().includes(q);
		})
	);

	// TanStack state
	let userSorting = $state<SortingState>([]);
	let userPagination = $state<PaginationState>({ pageIndex: 0, pageSize: 25 });

	let userColumns = $derived.by((): ColumnDef<User>[] => [
		{
			accessorKey: 'username',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Username',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(usernameCell, row.original),
			meta: { cellClass: 'px-4 py-3 font-medium' },
		},
		{
			id: 'canonicalId',
			header: 'Canonical ID',
			cell: ({ row }) => renderSnippet(canonicalIdCell, row.original),
			meta: { cellClass: 'px-4 py-3' },
		},
		{
			accessorKey: 'fullName',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Full Name',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => (row.original.fullName || '—') as string,
			meta: { cellClass: 'px-4 py-3 text-muted-foreground' },
		},
		{
			id: 'status',
			header: 'Status',
			cell: ({ row }) => renderSnippet(statusCell, row.original),
		},
		{
			id: 'roles',
			header: 'Roles',
			cell: ({ row }) => renderSnippet(rolesCell, row.original),
		},
		{
			id: 'namespaceAccess',
			header: 'Namespace Access',
			cell: ({ row }) => renderSnippet(nsAccessCell, row.original),
		},
	]);

	let usersTable = $derived(
		createSvelteTable({
			get data() {
				return filteredUsers;
			},
			get columns() {
				return userColumns;
			},
			state: {
				get sorting() {
					return userSorting;
				},
				get pagination() {
					return userPagination;
				},
			},
			onSortingChange: (updater) => {
				userSorting = typeof updater === 'function' ? updater(userSorting) : updater;
			},
			onPaginationChange: (updater) => {
				userPagination = typeof updater === 'function' ? updater(userPagination) : updater;
			},
			getCoreRowModel: getCoreRowModel(),
			getSortedRowModel: getSortedRowModel(),
			getPaginationRowModel: getPaginationRowModel(),
		})
	);
</script>

{#snippet usernameCell(user: User)}
	<a href="/users/{user.username}" class="text-primary underline-offset-4 hover:underline">
		{user.username}
	</a>
{/snippet}

{#snippet statusCell(user: User)}
	<Badge variant={user.enabled !== false ? 'default' : 'outline'}>
		{user.enabled !== false ? 'Active' : 'Disabled'}
	</Badge>
{/snippet}

{#snippet rolesCell(user: User)}
	{#each getUserRoles(user) as role (role)}
		<Badge variant="secondary" class="mr-1">{role}</Badge>
	{/each}
	{#if getUserRoles(user).length === 0}
		<span class="text-muted-foreground">—</span>
	{/if}
{/snippet}

{#snippet nsAccessCell(user: User)}
	{@const namespaces = getUserNamespaces(user.username)}
	{#if namespaces === undefined}
		<div class="h-5 w-20 animate-pulse rounded bg-muted"></div>
	{:else if namespaces.length > 0}
		<div class="flex flex-wrap gap-1">
			{#each namespaces as ns (ns)}
				<a href="/namespaces/{ns}">
					<Badge variant="outline" class="cursor-pointer hover:bg-accent">{ns}</Badge>
				</a>
			{/each}
		</div>
	{:else}
		<span class="text-muted-foreground">—</span>
	{/if}
{/snippet}

{#snippet canonicalIdCell(user: User)}
	{#if user.userGUID}
		<span class="flex items-center gap-1 font-mono text-xs text-muted-foreground">
			<span class="max-w-[120px] truncate">{user.userGUID}</span>
			<button
				class="shrink-0 rounded p-0.5 hover:bg-muted"
				title="Copy canonical ID"
				onclick={async (e) => {
					e.stopPropagation();
					e.preventDefault();
					try {
						await navigator.clipboard.writeText(user.userGUID!);
						toast.success('Canonical ID copied');
					} catch {
						const ta = document.createElement('textarea');
						ta.value = user.userGUID!;
						ta.style.position = 'fixed';
						ta.style.opacity = '0';
						document.body.appendChild(ta);
						ta.select();
						document.execCommand('copy');
						document.body.removeChild(ta);
						toast.success('Canonical ID copied');
					}
				}}
			>
				<Copy class="h-3 w-3" />
			</button>
		</span>
	{:else}
		<span class="text-muted-foreground">—</span>
	{/if}
{/snippet}

{#await usersData}
	<TableSkeleton rows={5} columns={6} />
{:then}
	<div class="flex items-center justify-between">
		<div class="relative max-w-md">
			<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
			<Input bind:value={userSearch} placeholder="Search users..." class="pl-10" />
		</div>
		<Button size="sm" class="ml-4" onclick={oncreate}>
			<Plus class="h-4 w-4" />
			Create User
		</Button>
	</div>
	<DataTable table={usersTable} noResultsMessage="No user accounts found." />
{/await}
