<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Textarea } from '$lib/components/ui/textarea/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Plus, Trash2 } from 'lucide-svelte';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import {
		get_email_notification,
		update_email_notification,
		type EmailNotification,
	} from '$lib/remote/tenant-info.remote.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	let emailData = $derived(get_email_notification({ tenant }));
	let email = $derived((emailData?.current ?? {}) as EmailNotification);

	const saver = useSave({
		successMsg: 'Email notification settings updated',
		errorMsg: 'Failed to update email notification settings',
	});

	let localEnabled = $state(false);
	let localFrom = $state('');
	let localSubject = $state('');
	let localBody = $state('');
	let localRecipients = $state<{ address: string; importance: string; severity: string }[]>([]);

	$effect(() => {
		const e = email;
		void saver.syncVersion;
		localEnabled = e.enabled ?? false;
		localFrom = e.emailTemplate?.from ?? '';
		localSubject = e.emailTemplate?.subject ?? '';
		localBody = e.emailTemplate?.body ?? '';
		localRecipients = (Array.isArray(e.recipients) ? e.recipients : []).map((r) => ({
			address: r.address ?? '',
			importance: r.importance ?? 'ALL',
			severity: r.severity ?? 'NOTICE',
		}));
	});

	function recipientsEqual(): boolean {
		const remote = Array.isArray(email.recipients) ? email.recipients : [];
		if (localRecipients.length !== remote.length) return false;
		return localRecipients.every(
			(r, i) =>
				r.address === (remote[i]?.address ?? '') &&
				r.importance === (remote[i]?.importance ?? 'ALL') &&
				r.severity === (remote[i]?.severity ?? 'NOTICE')
		);
	}

	let dirty = $derived(
		localEnabled !== (email.enabled ?? false) ||
			localFrom !== (email.emailTemplate?.from ?? '') ||
			localSubject !== (email.emailTemplate?.subject ?? '') ||
			localBody !== (email.emailTemplate?.body ?? '') ||
			!recipientsEqual()
	);

	function addRecipient() {
		localRecipients = [...localRecipients, { address: '', importance: 'ALL', severity: 'NOTICE' }];
	}

	function removeRecipient(index: number) {
		localRecipients = localRecipients.filter((_, i) => i !== index);
	}
</script>

{#await emailData}
	<div class="space-y-6">
		<CardSkeleton />
		<CardSkeleton />
	</div>
{:then}
	<div class="space-y-6">
		<!-- Enable + Template -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Email Template</Card.Title>
				<Card.Description>
					Configure the notification email template sent for system events.
				</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-4">
				<div class="flex items-center gap-2">
					<Switch id="email-enabled" bind:checked={localEnabled} />
					<Label for="email-enabled" class="text-sm">Email Notifications Enabled</Label>
				</div>
				<div class="grid gap-4 sm:grid-cols-2">
					<div class="space-y-1.5">
						<Label for="email-from">From Address</Label>
						<Input
							id="email-from"
							type="email"
							placeholder="noreply@example.com"
							bind:value={localFrom}
						/>
					</div>
					<div class="space-y-1.5">
						<Label for="email-subject">Subject</Label>
						<Input id="email-subject" placeholder="HCP Notification" bind:value={localSubject} />
					</div>
				</div>
				<div class="space-y-1.5">
					<Label for="email-body">Body Template</Label>
					<Textarea
						id="email-body"
						class="min-h-[80px]"
						placeholder="Notification body template..."
						bind:value={localBody}
					/>
				</div>
			</Card.Content>
		</Card.Root>

		<!-- Recipients -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Recipients</Card.Title>
				<Card.Description>
					Manage who receives email notifications and at what severity level.
				</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-3">
				{#each localRecipients as recipient, i (i)}
					<div class="flex items-end gap-2 rounded-md border p-3">
						<div class="min-w-0 flex-1 space-y-2">
							<div class="grid gap-2 sm:grid-cols-3">
								<div class="space-y-1">
									<Label class="text-xs">Email Address</Label>
									<Input
										class="h-8 text-sm"
										type="email"
										placeholder="user@example.com"
										bind:value={localRecipients[i].address}
									/>
								</div>
								<div class="space-y-1">
									<Label class="text-xs">Importance</Label>
									<select
										class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-full items-center rounded-md border px-3 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
										bind:value={localRecipients[i].importance}
									>
										<option value="ALL">All</option>
										<option value="MAJOR">Major Only</option>
									</select>
								</div>
								<div class="space-y-1">
									<Label class="text-xs">Severity</Label>
									<select
										class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-full items-center rounded-md border px-3 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
										bind:value={localRecipients[i].severity}
									>
										<option value="NOTICE">Notice</option>
										<option value="WARNING">Warning</option>
										<option value="ERROR">Error</option>
									</select>
								</div>
							</div>
						</div>
						<Button
							variant="ghost"
							size="icon"
							class="h-8 w-8 shrink-0 text-muted-foreground hover:text-destructive"
							onclick={() => removeRecipient(i)}
						>
							<Trash2 class="h-3.5 w-3.5" />
						</Button>
					</div>
				{/each}
				{#if localRecipients.length === 0}
					<p class="py-2 text-sm text-muted-foreground">
						No recipients configured. Add one to start receiving notifications.
					</p>
				{/if}
				<Button variant="outline" size="sm" onclick={addRecipient}>
					<Plus class="h-3.5 w-3.5" /> Add Recipient
				</Button>
			</Card.Content>
			<Card.Footer>
				<SaveButton
					{dirty}
					saving={saver.saving}
					onclick={() =>
						saver.run(async () => {
							if (!emailData) return;
							await update_email_notification({
								tenant,
								body: {
									enabled: localEnabled,
									emailTemplate: {
										from: localFrom || undefined,
										subject: localSubject || undefined,
										body: localBody || undefined,
									},
									recipients: localRecipients.filter((r) => r.address.trim()),
								},
							}).updates(emailData);
						})}
				/>
			</Card.Footer>
		</Card.Root>
	</div>
{/await}
