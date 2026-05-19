<script lang="ts">
	import { Search, X, Loader2 } from 'lucide-svelte';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import { toast } from 'svelte-sonner';
	import {
		get_buckets,
		get_bucket_acl,
		put_bucket_acl,
		type AclGrant,
		type AclData,
	} from '$lib/remote/buckets.remote.js';
	import { get_users } from '$lib/remote/users.remote.js';
	import { get_namespaces, type Namespace } from '$lib/remote/namespaces.remote.js';
	import type { User } from '$lib/constants.js';
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
	import { PERMISSION_MAP, permissionColor, permissionLabel } from '../acl-constants.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	interface Props {
		tenant?: string;
	}

	let { tenant }: Props = $props();

	// Grouped row: one row per (bucket + grantee), with all permissions merged
	type GroupedGrantRow = {
		bucket: string;
		owner: string;
		granteeDisplay: string;
		granteeType: string;
		granteeId: string;
		permissions: { value: string; grantIndex: number }[];
	};

	// Data fetching
	let bucketData = get_buckets();
	let s3Buckets = $derived(
		(bucketData.current?.buckets ?? []) as { name: string; creation_date: string }[]
	);
	let nsData = $derived(tenant ? get_namespaces({ tenant }) : undefined);
	let namespaces = $derived((nsData?.current ?? []) as Namespace[]);

	// Merge S3 buckets with all accessible namespaces
	let buckets = $derived.by(() => {
		const s3Set = new Set(s3Buckets.map((b) => b.name));
		const merged = [...s3Buckets];
		for (const ns of namespaces) {
			if (!s3Set.has(ns.name)) {
				merged.push({ name: ns.name, creation_date: ns.creationTime ?? '' });
			}
		}
		return merged;
	});

	// Fetch ACL for each bucket using a reactive map
	let bucketAcls = $derived.by(() => {
		const map = new SvelteMap<string, ReturnType<typeof get_bucket_acl>>();
		for (const b of buckets) {
			map.set(b.name, get_bucket_acl({ bucket: b.name }));
		}
		return map;
	});

	// Fetch users for username resolution when tenant available
	let usersData = $derived(tenant ? get_users({ tenant }) : null);
	let guidToUsername = $derived.by(() => {
		const map = new SvelteMap<string, string>();
		const userList = (usersData?.current ?? []) as User[];
		for (const u of userList) {
			if (u.userGUID) {
				map.set(u.userGUID, u.username);
			}
		}
		return map;
	});

	function resolveGrantee(grant: AclGrant): {
		display: string;
		type: string;
		id: string;
	} {
		const g = grant.Grantee ?? {};
		const type = (g.Type as string) ?? 'CanonicalUser';
		if (type === 'Group') {
			const uri = (g.URI as string) ?? '';
			const label = uri.split('/').pop() || uri || 'Unknown';
			return { display: label, type: 'Group', id: uri };
		}
		const id = (g.ID as string) || '';
		const displayName = (g.DisplayName as string) || '';
		const username = guidToUsername.get(id);
		if (username) {
			return { display: username, type: 'User', id };
		}
		return { display: displayName || id || 'Unknown', type: 'User', id };
	}

	// Derive grouped rows: one row per (bucket + grantee)
	let rows = $derived.by((): GroupedGrantRow[] => {
		const result: GroupedGrantRow[] = [];
		for (const b of buckets) {
			const aclQuery = bucketAcls.get(b.name);
			const acl = (aclQuery?.current ?? { owner: null, grants: [] }) as AclData;
			const owner = acl.owner?.DisplayName || acl.owner?.ID || '';
			const grants = acl.grants ?? [];

			// Group grants by grantee ID within this bucket
			const granteeMap = new Map<string, GroupedGrantRow>();
			for (let i = 0; i < grants.length; i++) {
				const grant = grants[i];
				const { display, type, id } = resolveGrantee(grant);
				let group = granteeMap.get(id);
				if (!group) {
					group = {
						bucket: b.name,
						owner,
						granteeDisplay: display,
						granteeType: type,
						granteeId: id,
						permissions: [],
					};
					granteeMap.set(id, group);
				}
				group.permissions.push({ value: grant.Permission ?? '', grantIndex: i });
			}
			result.push(...granteeMap.values());
		}
		return result;
	});

	// Filters
	let searchBucket = $state('');
	let searchGrantee = $state('');
	let filterPermission = $state('');
	let filterType = $state('');

	let filteredRows = $derived(
		rows.filter((r) => {
			if (searchBucket && !r.bucket.toLowerCase().includes(searchBucket.toLowerCase()))
				return false;
			if (searchGrantee && !r.granteeDisplay.toLowerCase().includes(searchGrantee.toLowerCase()))
				return false;
			if (filterPermission && !r.permissions.some((p) => p.value === filterPermission))
				return false;
			if (filterType && r.granteeType !== filterType) return false;
			return true;
		})
	);

	let allPermissions = $derived(
		[...new Set(rows.flatMap((r) => r.permissions.map((p) => p.value)).filter(Boolean))].sort()
	);
	let allTypes = $derived([...new Set(rows.map((r) => r.granteeType).filter(Boolean))].sort());

	// Revoke handler - revokes a single permission from a grantee on a bucket
	let revoking = $state<string | null>(null);

	function revokeKey(bucketName: string, granteeId: string, permission: string): string {
		return `${bucketName}:${granteeId}:${permission}`;
	}

	async function handleRevoke(row: GroupedGrantRow, permValue: string, grantIndex: number) {
		const key = revokeKey(row.bucket, row.granteeId, permValue);
		revoking = key;
		try {
			const aclQuery = bucketAcls.get(row.bucket);
			const currentAcl = (aclQuery?.current ?? { owner: null, grants: [] }) as AclData;

			const remainingGrants = currentAcl.grants
				.filter((_, i) => i !== grantIndex)
				.map((g) => ({ Grantee: g.Grantee, Permission: g.Permission }));

			const result = put_bucket_acl({
				bucket: row.bucket,
				owner: currentAcl.owner
					? { ID: currentAcl.owner.ID, DisplayName: currentAcl.owner.DisplayName }
					: undefined,
				grants: remainingGrants,
			});
			if (aclQuery) await result.updates(aclQuery);
			else await result;

			toast.success(`Revoked ${permissionLabel(permValue)} on ${row.bucket}`);
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to revoke grant'));
		} finally {
			revoking = null;
		}
	}

	// TanStack Table state
	let sorting = $state<SortingState>([]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 25 });

	let columns = $derived.by((): ColumnDef<GroupedGrantRow>[] => [
		{
			accessorKey: 'bucket',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Bucket',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(bucketNameCell, row.original),
			meta: { cellClass: 'px-4 py-3 font-medium' },
		},
		{
			accessorKey: 'granteeDisplay',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Grantee',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(granteeCell, row.original),
			meta: { cellClass: 'px-4 py-3' },
		},
		{
			accessorKey: 'granteeType',
			header: 'Type',
			cell: ({ row }) => renderSnippet(typeCell, row.original),
			meta: { cellClass: 'px-4 py-3' },
		},
		{
			id: 'permissions',
			header: 'Permissions',
			cell: ({ row }) => renderSnippet(permissionsCell, row.original),
			meta: { cellClass: 'px-4 py-3' },
		},
	]);

	let table = $derived(
		createSvelteTable({
			get data() {
				return filteredRows;
			},
			get columns() {
				return columns;
			},
			state: {
				get sorting() {
					return sorting;
				},
				get pagination() {
					return pagination;
				},
			},
			onSortingChange: (updater) => {
				sorting = typeof updater === 'function' ? updater(sorting) : updater;
			},
			onPaginationChange: (updater) => {
				pagination = typeof updater === 'function' ? updater(pagination) : updater;
			},
			getCoreRowModel: getCoreRowModel(),
			getSortedRowModel: getSortedRowModel(),
			getPaginationRowModel: getPaginationRowModel(),
		})
	);

	let noResultsMessage = $derived(
		rows.length === 0 ? 'No grants found.' : 'No results matching current filters'
	);
</script>

{#snippet bucketNameCell(row: GroupedGrantRow)}
	<a
		href="/buckets/{row.bucket}"
		class="text-primary underline-offset-4 hover:underline"
		onclick={(e) => e.stopPropagation()}
	>
		{row.bucket}
	</a>
{/snippet}

{#snippet granteeCell(row: GroupedGrantRow)}
	<div class="flex flex-col gap-0.5">
		<span>{row.granteeDisplay}</span>
		{#if row.granteeId && row.granteeDisplay !== row.granteeId}
			<span class="truncate font-mono text-[11px] text-muted-foreground" title={row.granteeId}>
				{row.granteeId}
			</span>
		{/if}
	</div>
{/snippet}

{#snippet typeCell(row: GroupedGrantRow)}
	<Badge variant="outline">{row.granteeType}</Badge>
{/snippet}

{#snippet permissionsCell(row: GroupedGrantRow)}
	<div class="flex flex-wrap gap-1.5">
		{#each row.permissions as perm (perm.value)}
			{@const key = revokeKey(row.bucket, row.granteeId, perm.value)}
			<Tooltip.Root>
				<Tooltip.Trigger>
					{#snippet child({ props })}
						<span {...props} class="inline-flex items-center gap-0.5">
							<Badge variant={permissionColor(perm.value)} class="pr-1">
								{permissionLabel(perm.value)}
								<button
									class="ml-1 inline-flex h-3.5 w-3.5 items-center justify-center rounded-full hover:bg-black/20 dark:hover:bg-white/20"
									disabled={revoking === key}
									onclick={(e) => {
										e.stopPropagation();
										handleRevoke(row, perm.value, perm.grantIndex);
									}}
									title="Revoke"
								>
									{#if revoking === key}
										<Loader2 class="h-2.5 w-2.5 animate-spin" />
									{:else}
										<X class="h-2.5 w-2.5" />
									{/if}
								</button>
							</Badge>
						</span>
					{/snippet}
				</Tooltip.Trigger>
				<Tooltip.Content side="top" class="max-w-xs">
					{PERMISSION_MAP.get(perm.value)?.description ?? perm.value}
				</Tooltip.Content>
			</Tooltip.Root>
		{/each}
	</div>
{/snippet}

{#await bucketData}
	<TableSkeleton rows={5} columns={4} />
{:then}
	<div class="space-y-2">
		<div class="flex flex-wrap items-center gap-3">
			<div class="relative max-w-md">
				<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
				<Input bind:value={searchBucket} placeholder="Filter by bucket..." class="pl-10" />
			</div>
			<div class="relative max-w-md">
				<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
				<Input bind:value={searchGrantee} placeholder="Filter by grantee..." class="pl-10" />
			</div>
			{#if allTypes.length > 1}
				<select
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-36 items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1"
					bind:value={filterType}
				>
					<option value="">All types</option>
					{#each allTypes as t (t)}
						<option value={t}>{t}</option>
					{/each}
				</select>
			{/if}
			{#if allPermissions.length > 1}
				<select
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-48 items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1"
					bind:value={filterPermission}
				>
					<option value="">All permissions</option>
					{#each allPermissions as p (p)}
						<option value={p}>{permissionLabel(p)}</option>
					{/each}
				</select>
			{/if}
		</div>
		<div class="flex items-center">
			<span class="ml-auto text-xs text-muted-foreground">
				{filteredRows.length} of {rows.length} grantees across {new Set(rows.map((r) => r.bucket))
					.size}
				buckets
			</span>
		</div>
	</div>

	<DataTable {table} {noResultsMessage}>
		{#snippet footer()}
			{filteredRows.length} grantee(s) across {new Set(rows.map((r) => r.bucket)).size} buckets
		{/snippet}
	</DataTable>
{/await}
