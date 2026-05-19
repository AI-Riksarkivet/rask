<script lang="ts">
	import { page } from '$app/state';
	import { Plus, Search } from 'lucide-svelte';
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { toast } from 'svelte-sonner';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import {
		get_content_classes,
		create_content_class,
		delete_content_class,
		type ContentClass,
	} from '$lib/remote/content-classes.remote.js';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import ErrorBanner from '$lib/components/custom/error-banner/error-banner.svelte';
	import NoTenantPlaceholder from '$lib/components/custom/no-tenant-placeholder/no-tenant-placeholder.svelte';
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
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let tenant = $derived(page.data.tenant as string | undefined);
	let classesData = $derived(tenant ? get_content_classes({ tenant }) : undefined);
	let classes = $derived((classesData?.current ?? []) as ContentClass[]);

	// Search
	let searchQuery = $state('');
	let filtered = $derived(
		classes.filter((c) => {
			const q = searchQuery.toLowerCase();
			if (!q) return true;
			return c.name.toLowerCase().includes(q);
		})
	);

	// TanStack state
	let sorting = $state<SortingState>([]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 25 });

	let columns = $derived.by((): ColumnDef<ContentClass>[] => [
		{
			accessorKey: 'name',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'Name',
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(nameCell, row.original),
			meta: { cellClass: 'px-4 py-3 font-medium' },
		},
		{
			id: 'properties',
			header: 'Properties',
			cell: ({ row }) => renderSnippet(propsCell, row.original),
			meta: { cellClass: 'px-4 py-3' },
		},
		{
			id: 'namespaces',
			header: 'Namespaces',
			cell: ({ row }) => renderSnippet(nsCell, row.original),
			meta: { cellClass: 'px-4 py-3' },
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) => renderSnippet(actionsCell, row.original),
			meta: { cellClass: 'px-4 py-3 w-[50px]' },
		},
	]);

	let table = $derived(
		createSvelteTable({
			get data() {
				return filtered;
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

	// Create dialog
	let createOpen = $state(false);
	let createError = $state('');
	let creating = $state(false);

	async function handleCreate(e: SubmitEvent) {
		e.preventDefault();
		if (!tenant || !classesData) return;
		const form = e.currentTarget as HTMLFormElement;
		const fd = new FormData(form);
		const name = fd.get('name') as string;
		if (!name) return;
		creating = true;
		createError = '';
		try {
			await create_content_class({ tenant, name }).updates(classesData);
			toast.success(`Content class "${name}" created`);
			createOpen = false;
			form.reset();
		} catch (err) {
			createError = getErrorMessage(err, 'Failed to create content class');
		} finally {
			creating = false;
		}
	}

	// Delete
	let deleteTarget = $state<string | null>(null);
	let deleteOpen = $state(false);
	let deleting = $state(false);

	async function handleDelete() {
		if (!tenant || !classesData || !deleteTarget) return;
		deleting = true;
		try {
			await delete_content_class({ tenant, name: deleteTarget }).updates(classesData);
			toast.success(`Content class "${deleteTarget}" deleted`);
			deleteOpen = false;
			deleteTarget = null;
		} catch {
			toast.error('Failed to delete content class');
		} finally {
			deleting = false;
		}
	}
</script>

{#snippet nameCell(cc: ContentClass)}
	<a href="/content-classes/{cc.name}" class="text-primary underline-offset-4 hover:underline">
		{cc.name}
	</a>
{/snippet}

{#snippet propsCell(cc: ContentClass)}
	{@const count = cc.contentProperties?.length ?? 0}
	<Badge variant="secondary">{count} {count === 1 ? 'property' : 'properties'}</Badge>
{/snippet}

{#snippet nsCell(cc: ContentClass)}
	{@const nsList = cc.namespaces ?? []}
	{#if nsList.length > 0}
		<div class="flex flex-wrap gap-1">
			{#each nsList as ns (ns)}
				<a href="/namespaces/{ns}">
					<Badge variant="outline" class="cursor-pointer hover:bg-accent">{ns}</Badge>
				</a>
			{/each}
		</div>
	{:else}
		<span class="text-muted-foreground">—</span>
	{/if}
{/snippet}

{#snippet actionsCell(cc: ContentClass)}
	<Button
		variant="ghost"
		size="sm"
		class="h-8 text-muted-foreground hover:text-destructive"
		onclick={() => {
			deleteTarget = cc.name;
			deleteOpen = true;
		}}
	>
		Delete
	</Button>
{/snippet}

<svelte:head>
	<title>Content Classes - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="Content Classes"
		description="Manage metadata schemas for object indexing and search"
	/>

	{#if tenant}
		{#await classesData}
			<TableSkeleton rows={3} columns={4} />
		{:then}
			<div class="flex items-center justify-between">
				<div class="relative max-w-md">
					<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
					<Input bind:value={searchQuery} placeholder="Search content classes..." class="pl-10" />
				</div>
				<Button size="sm" class="ml-4" onclick={() => (createOpen = true)}>
					<Plus class="h-4 w-4" />
					Create Content Class
				</Button>
			</div>
			<DataTable {table} noResultsMessage="No content classes found." />
		{/await}
	{:else}
		<NoTenantPlaceholder message="Log in with a tenant to manage content classes." />
	{/if}
</div>

<!-- Create Dialog -->
<Dialog.Root bind:open={createOpen}>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Header>
			<Dialog.Title>Create Content Class</Dialog.Title>
			<Dialog.Description>
				Define a new metadata schema. You can add properties after creation.
			</Dialog.Description>
		</Dialog.Header>
		<form onsubmit={handleCreate} class="space-y-4">
			<ErrorBanner message={createError} />
			<div class="space-y-2">
				<Label for="cc-name">Name</Label>
				<Input id="cc-name" name="name" placeholder="document-metadata" required />
			</div>
			<Dialog.Footer>
				<Button variant="ghost" type="button" onclick={() => (createOpen = false)}>Cancel</Button>
				<Button type="submit" disabled={creating}>
					{creating ? 'Creating...' : 'Create'}
				</Button>
			</Dialog.Footer>
		</form>
	</Dialog.Content>
</Dialog.Root>

<DeleteConfirmDialog
	bind:open={deleteOpen}
	name={deleteTarget ?? ''}
	itemType="content class"
	loading={deleting}
	onconfirm={handleDelete}
/>
