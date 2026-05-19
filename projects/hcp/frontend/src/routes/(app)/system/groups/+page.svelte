<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Table from '$lib/components/ui/table/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import { get_system_groups, type SystemGroup } from '$lib/remote/system.remote.js';

	let groupsData = $derived(get_system_groups({}));
	let groups = $derived((groupsData?.current ?? []) as SystemGroup[]);
</script>

<svelte:head>
	<title>System Groups - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="System Group Accounts"
		description="View system-level group accounts and their roles"
	/>

	{#await groupsData}
		<CardSkeleton />
	{:then}
		<Card.Root>
			<Card.Header>
				<Card.Title>Group Accounts</Card.Title>
				<Card.Description>
					{groups.length} system group{groups.length !== 1 ? 's' : ''}
				</Card.Description>
			</Card.Header>
			<Card.Content>
				{#if groups.length === 0}
					<p class="py-8 text-center text-sm text-muted-foreground">No system groups found.</p>
				{:else}
					<Table.Root>
						<Table.Header>
							<Table.Row>
								<Table.Head>Group Name</Table.Head>
								<Table.Head>External Group ID</Table.Head>
								<Table.Head>Roles</Table.Head>
								<Table.Head>Namespace Management</Table.Head>
							</Table.Row>
						</Table.Header>
						<Table.Body>
							{#each groups as group (group.groupname)}
								<Table.Row>
									<Table.Cell class="font-medium">{group.groupname ?? '—'}</Table.Cell>
									<Table.Cell class="text-muted-foreground">
										{group.externalGroupID || '—'}
									</Table.Cell>
									<Table.Cell>
										<div class="flex flex-wrap gap-1">
											{#each group.roles?.role ?? [] as role (role)}
												<Badge variant="outline" class="text-xs">{role}</Badge>
											{/each}
										</div>
									</Table.Cell>
									<Table.Cell>
										{#if group.allowNamespaceManagement}
											<Badge variant="default">Allowed</Badge>
										{:else}
											<Badge variant="secondary">Not Allowed</Badge>
										{/if}
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
