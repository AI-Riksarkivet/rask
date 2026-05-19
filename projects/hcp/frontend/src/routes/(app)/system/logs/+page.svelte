<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import { toast } from 'svelte-sonner';
	import {
		get_log_status,
		mark_logs,
		prepare_logs,
		cancel_log_download,
		type LogStatus,
	} from '$lib/remote/system.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let statusData = $derived(get_log_status({}));
	let status = $derived((statusData?.current ?? {}) as LogStatus);

	let markMessage = $state('');
	let markLoading = $state(false);
	let prepareLoading = $state(false);
	let startDate = $state('');
	let endDate = $state('');

	async function handleMark() {
		if (!markMessage.trim()) return;
		markLoading = true;
		try {
			await mark_logs({ message: markMessage }).updates(statusData);
			toast.success('Logs marked successfully');
			markMessage = '';
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to mark logs'));
		} finally {
			markLoading = false;
		}
	}

	async function handlePrepare() {
		prepareLoading = true;
		try {
			await prepare_logs({
				startDate: startDate || undefined,
				endDate: endDate || undefined,
			}).updates(statusData);
			toast.success('Log package preparation started');
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to prepare logs'));
		} finally {
			prepareLoading = false;
		}
	}

	async function handleCancel() {
		try {
			await cancel_log_download({}).updates(statusData);
			toast.success('Log download cancelled');
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to cancel'));
		}
	}
</script>

<svelte:head>
	<title>Logs - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="Log Management"
		description="Prepare, download, and manage system log packages"
	/>

	{#await statusData}
		<div class="space-y-6">
			<CardSkeleton />
			<CardSkeleton />
		</div>
	{:then}
		<div class="grid gap-6 lg:grid-cols-2">
			<!-- Status -->
			<Card.Root>
				<Card.Header>
					<Card.Title>Download Status</Card.Title>
					<Card.Description>Current state of log package preparation.</Card.Description>
				</Card.Header>
				<Card.Content>
					<dl class="grid grid-cols-[auto_1fr] gap-x-6 gap-y-3 text-sm">
						<dt class="text-muted-foreground">Started</dt>
						<dd>
							{#if status.started}
								<Badge variant="default">Yes</Badge>
							{:else}
								<Badge variant="secondary">No</Badge>
							{/if}
						</dd>

						<dt class="text-muted-foreground">Ready for Download</dt>
						<dd>
							{#if status.readyForStreaming}
								<Badge variant="default">Ready</Badge>
							{:else}
								<Badge variant="secondary">Not Ready</Badge>
							{/if}
						</dd>

						<dt class="text-muted-foreground">Streaming</dt>
						<dd>
							{#if status.streamingInProgress}
								<Badge variant="default">In Progress</Badge>
							{:else}
								<Badge variant="secondary">Idle</Badge>
							{/if}
						</dd>

						<dt class="text-muted-foreground">Error</dt>
						<dd>
							{#if status.error}
								<Badge variant="destructive">Error</Badge>
							{:else}
								<Badge variant="secondary">None</Badge>
							{/if}
						</dd>
					</dl>

					{#if status.started}
						<div class="mt-4">
							<Button variant="outline" size="sm" onclick={handleCancel}>Cancel Download</Button>
						</div>
					{/if}
				</Card.Content>
			</Card.Root>

			<!-- Mark Logs -->
			<Card.Root>
				<Card.Header>
					<Card.Title>Mark Logs</Card.Title>
					<Card.Description>
						Insert a marker message into the system logs for later reference.
					</Card.Description>
				</Card.Header>
				<Card.Content class="space-y-4">
					<div class="space-y-1.5">
						<Label for="mark-msg">Message</Label>
						<Input id="mark-msg" bind:value={markMessage} placeholder="Enter a marker message..." />
					</div>
				</Card.Content>
				<Card.Footer>
					<Button onclick={handleMark} disabled={markLoading || !markMessage.trim()}>
						{markLoading ? 'Marking...' : 'Mark Logs'}
					</Button>
				</Card.Footer>
			</Card.Root>
		</div>

		<!-- Prepare Logs -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Prepare Log Package</Card.Title>
				<Card.Description>Specify a date range and prepare logs for download.</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-4">
				<div class="grid gap-4 sm:grid-cols-2">
					<div class="space-y-1.5">
						<Label for="log-start">Start Date</Label>
						<Input id="log-start" type="date" bind:value={startDate} />
					</div>
					<div class="space-y-1.5">
						<Label for="log-end">End Date</Label>
						<Input id="log-end" type="date" bind:value={endDate} />
					</div>
				</div>
			</Card.Content>
			<Card.Footer>
				<Button onclick={handlePrepare} disabled={prepareLoading}>
					{prepareLoading ? 'Preparing...' : 'Prepare Logs'}
				</Button>
			</Card.Footer>
		</Card.Root>
	{/await}
</div>
