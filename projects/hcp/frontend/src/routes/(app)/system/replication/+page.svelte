<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Table from '$lib/components/ui/table/index.js';

	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import FormDialog from '$lib/components/custom/form-dialog/form-dialog.svelte';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import { Plus } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { formatBytes, formatDate } from '$lib/utils/format.js';
	import {
		get_replication_service,
		get_replication_certificates,
		delete_replication_certificate,
		get_replication_links,
		create_replication_link,
		delete_replication_link,
		action_replication_link,
		type ReplicationServiceSettings,
		type ReplicationCertificate,
		type ReplicationLink,
	} from '$lib/remote/replication.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let serviceData = $derived(get_replication_service({}));
	let service = $derived((serviceData?.current ?? {}) as ReplicationServiceSettings);

	let certsData = $derived(get_replication_certificates({}));
	let certificates = $derived((certsData?.current ?? []) as ReplicationCertificate[]);

	let linksData = $derived(get_replication_links({}));
	let links = $derived((linksData?.current ?? []) as ReplicationLink[]);

	// Create link dialog state
	let createOpen = $state(false);
	let createName = $state('');
	let createType = $state('ACTIVE_ACTIVE');
	let createHost = $state('');
	let createPort = $state('');
	let createCompression = $state(false);
	let createEncryption = $state(true);
	let createError = $state('');
	let createLoading = $state(false);

	// Delete link dialog state
	let deleteLinkOpen = $state(false);
	let deleteLinkName = $state('');

	// Delete certificate dialog state
	let deleteCertOpen = $state(false);
	let deleteCertId = $state('');

	async function handleCreateLink(e: SubmitEvent) {
		e.preventDefault();
		createLoading = true;
		createError = '';
		try {
			await create_replication_link({
				body: {
					name: createName,
					type: createType,
					connection: {
						remoteHost: createHost,
						remotePort: createPort ? Number(createPort) : 5748,
					},
					compression: createCompression,
					encryption: createEncryption,
				},
			}).updates(linksData);
			toast.success(`Replication link "${createName}" created`);
			createOpen = false;
			createName = '';
			createType = 'ACTIVE_ACTIVE';
			createHost = '';
			createPort = '';
			createCompression = false;
			createEncryption = true;
		} catch (err) {
			createError = getErrorMessage(err, 'Failed to create replication link');
		} finally {
			createLoading = false;
		}
	}

	async function handleDeleteLink() {
		try {
			await delete_replication_link({ linkName: deleteLinkName }).updates(linksData);
			toast.success(`Replication link "${deleteLinkName}" deleted`);
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to delete replication link'));
		}
	}

	async function handleDeleteCert() {
		try {
			await delete_replication_certificate({ certificateId: deleteCertId }).updates(certsData);
			toast.success('Certificate deleted');
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to delete certificate'));
		}
	}

	async function handleLinkAction(linkName: string, action: string) {
		try {
			await action_replication_link({
				linkName,
				action: action as
					| 'suspend'
					| 'resume'
					| 'failOver'
					| 'failBack'
					| 'beginRecover'
					| 'completeRecovery',
			}).updates(linksData);
			toast.success(
				`Replication link "${linkName}" ${action === 'suspend' ? 'suspended' : 'resumed'}`
			);
		} catch (err) {
			toast.error(getErrorMessage(err, `Failed to ${action} replication link`));
		}
	}

	function linkStatusVariant(status: string): 'default' | 'destructive' | 'secondary' {
		if (status === 'ACTIVE') return 'default';
		if (status === 'SUSPENDED') return 'destructive';
		return 'secondary';
	}

	function linkTypeVariant(type: string): 'default' | 'secondary' | 'outline' {
		if (type === 'ACTIVE_ACTIVE') return 'default';
		if (type === 'OUTBOUND_ONLY') return 'secondary';
		return 'outline';
	}
</script>

<svelte:head>
	<title>Replication - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="Replication Management"
		description="Manage replication links, certificates, and service settings"
	>
		{#snippet actions()}
			<Button onclick={() => (createOpen = true)}>
				<Plus class="mr-1.5 h-4 w-4" />
				Create Link
			</Button>
		{/snippet}
	</PageHeader>

	<!-- Service Settings Card -->
	{#await serviceData}
		<CardSkeleton />
	{:then}
		<Card.Root>
			<Card.Header>
				<Card.Title>Service Settings</Card.Title>
				<Card.Description>Replication service configuration and status</Card.Description>
			</Card.Header>
			<Card.Content>
				<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
					<div class="space-y-1">
						<p class="text-sm font-medium text-muted-foreground">Service Status</p>
						<Badge variant={service.status === 'RUNNING' ? 'default' : 'secondary'}>
							{service.status ?? 'UNKNOWN'}
						</Badge>
					</div>
					<div class="space-y-1">
						<p class="text-sm font-medium text-muted-foreground">Verification Mode</p>
						<p class="text-sm">{service.verification ?? '—'}</p>
					</div>
					<div class="space-y-1">
						<p class="text-sm font-medium text-muted-foreground">Connectivity Timeout</p>
						<p class="text-sm">
							{service.connectivityTimeoutSeconds != null
								? `${service.connectivityTimeoutSeconds}s`
								: '—'}
						</p>
					</div>
					<div class="space-y-1">
						<p class="text-sm font-medium text-muted-foreground">DNS Failover</p>
						<Badge variant={service.enableDNSFailover ? 'default' : 'secondary'}>
							{service.enableDNSFailover ? 'Enabled' : 'Disabled'}
						</Badge>
					</div>
				</div>
			</Card.Content>
		</Card.Root>
	{/await}

	<!-- Certificates Card -->
	{#await certsData}
		<CardSkeleton />
	{:then}
		<Card.Root>
			<Card.Header>
				<Card.Title>Certificates</Card.Title>
				<Card.Description>
					{certificates.length} certificate{certificates.length !== 1 ? 's' : ''} configured
				</Card.Description>
			</Card.Header>
			<Card.Content>
				{#if certificates.length === 0}
					<p class="py-8 text-center text-sm text-muted-foreground">
						No replication certificates found.
					</p>
				{:else}
					<Table.Root>
						<Table.Header>
							<Table.Row>
								<Table.Head>ID</Table.Head>
								<Table.Head>Subject DN</Table.Head>
								<Table.Head>Valid On</Table.Head>
								<Table.Head>Expires On</Table.Head>
								<Table.Head class="w-[80px]"></Table.Head>
							</Table.Row>
						</Table.Header>
						<Table.Body>
							{#each certificates as cert (cert.id)}
								<Table.Row>
									<Table.Cell class="font-mono text-sm">{cert.id ?? '---'}</Table.Cell>
									<Table.Cell class="max-w-[250px] truncate">{cert.subjectDN ?? '---'}</Table.Cell>
									<Table.Cell>{cert.validOn ? formatDate(cert.validOn) : '---'}</Table.Cell>
									<Table.Cell>{cert.expiresOn ? formatDate(cert.expiresOn) : '---'}</Table.Cell>
									<Table.Cell>
										<Button
											variant="ghost"
											size="sm"
											class="text-destructive"
											onclick={() => {
												deleteCertId = cert.id ?? '';
												deleteCertOpen = true;
											}}
										>
											Delete
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

	<!-- Links Card -->
	{#await linksData}
		<CardSkeleton />
	{:then}
		<Card.Root>
			<Card.Header>
				<Card.Title>Replication Links</Card.Title>
				<Card.Description>
					{links.length} replication link{links.length !== 1 ? 's' : ''} configured
				</Card.Description>
			</Card.Header>
			<Card.Content>
				{#if links.length === 0}
					<p class="py-8 text-center text-sm text-muted-foreground">No replication links found.</p>
				{:else}
					<Table.Root>
						<Table.Header>
							<Table.Row>
								<Table.Head>Name</Table.Head>
								<Table.Head>Type</Table.Head>
								<Table.Head>Status</Table.Head>
								<Table.Head>Host</Table.Head>
								<Table.Head>Compression</Table.Head>
								<Table.Head>Encryption</Table.Head>
								<Table.Head>Pending Objects</Table.Head>
								<Table.Head>Bytes Pending</Table.Head>
								<Table.Head class="w-[150px]"></Table.Head>
							</Table.Row>
						</Table.Header>
						<Table.Body>
							{#each links as link (link.name)}
								<Table.Row>
									<Table.Cell class="font-medium">
										<a
											href="/system/replication/{link.name}"
											class="text-primary underline-offset-4 hover:underline"
										>
											{link.name ?? '---'}
										</a>
									</Table.Cell>
									<Table.Cell>
										<Badge variant={linkTypeVariant(link.type ?? '')}>
											{link.type ?? '---'}
										</Badge>
									</Table.Cell>
									<Table.Cell>
										<Badge variant={linkStatusVariant(link.status ?? '')}>
											{link.status ?? '---'}
										</Badge>
									</Table.Cell>
									<Table.Cell>{link.connection?.remoteHost ?? '—'}</Table.Cell>
									<Table.Cell>
										<Badge variant={link.compression ? 'default' : 'secondary'}>
											{link.compression ? 'On' : 'Off'}
										</Badge>
									</Table.Cell>
									<Table.Cell>
										<Badge variant={link.encryption ? 'default' : 'secondary'}>
											{link.encryption ? 'On' : 'Off'}
										</Badge>
									</Table.Cell>
									<Table.Cell>
										{link.statistics?.objectsPending != null
											? link.statistics.objectsPending.toLocaleString()
											: '—'}
									</Table.Cell>
									<Table.Cell>
										{link.statistics?.bytesPending != null
											? formatBytes(link.statistics.bytesPending)
											: '—'}
									</Table.Cell>
									<Table.Cell>
										<div class="flex gap-1">
											{#if link.status === 'ACTIVE'}
												<Button
													variant="ghost"
													size="sm"
													onclick={() => handleLinkAction(link.name ?? '', 'suspend')}
												>
													Suspend
												</Button>
											{:else if link.status === 'SUSPENDED'}
												<Button
													variant="ghost"
													size="sm"
													onclick={() => handleLinkAction(link.name ?? '', 'resume')}
												>
													Resume
												</Button>
											{/if}
											<Button
												variant="ghost"
												size="sm"
												class="text-destructive"
												onclick={() => {
													deleteLinkName = link.name ?? '';
													deleteLinkOpen = true;
												}}
											>
												Delete
											</Button>
										</div>
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
	bind:open={createOpen}
	title="Create Replication Link"
	loading={createLoading}
	error={createError}
	onsubmit={handleCreateLink}
>
	<div class="space-y-4">
		<div class="space-y-1.5">
			<Label for="link-name">Link Name</Label>
			<Input id="link-name" bind:value={createName} required placeholder="my-replication-link" />
		</div>
		<div class="space-y-1.5">
			<Label for="link-type">Link Type</Label>
			<select
				id="link-type"
				class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
				bind:value={createType}
			>
				<option value="ACTIVE_ACTIVE">ACTIVE_ACTIVE</option>
				<option value="OUTBOUND_ONLY">OUTBOUND_ONLY</option>
				<option value="INBOUND_ONLY">INBOUND_ONLY</option>
			</select>
		</div>
		<div class="space-y-1.5">
			<Label for="link-host">Remote Host</Label>
			<Input id="link-host" bind:value={createHost} required placeholder="remote-hcp.example.com" />
		</div>
		<div class="space-y-1.5">
			<Label for="link-port">Remote Port</Label>
			<Input id="link-port" type="number" bind:value={createPort} placeholder="443" />
		</div>
		<hr />
		<div class="flex items-center gap-2">
			<Switch id="link-compression" bind:checked={createCompression} />
			<Label for="link-compression" class="text-sm">Enable Compression</Label>
		</div>
		<div class="flex items-center gap-2">
			<Switch id="link-encryption" bind:checked={createEncryption} />
			<Label for="link-encryption" class="text-sm">Enable Encryption</Label>
		</div>
	</div>
</FormDialog>

<DeleteConfirmDialog
	bind:open={deleteLinkOpen}
	name={deleteLinkName}
	itemType="Replication Link"
	description="This action cannot be undone. The replication link and its configuration will be permanently removed."
	onconfirm={handleDeleteLink}
/>

<DeleteConfirmDialog
	bind:open={deleteCertOpen}
	name={deleteCertId}
	itemType="Certificate"
	description="This action cannot be undone. The replication certificate will be permanently removed."
	onconfirm={handleDeleteCert}
/>
