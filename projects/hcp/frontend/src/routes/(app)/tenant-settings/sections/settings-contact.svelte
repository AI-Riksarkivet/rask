<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import {
		get_tenant_settings,
		update_contact_info,
		type TenantSettings,
	} from '$lib/remote/tenant-info.remote.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	let settingsData = $derived(get_tenant_settings({ tenant }));
	let settings = $derived((settingsData?.current ?? null) as TenantSettings | null);

	const saver = useSave({
		successMsg: 'Contact info updated successfully',
		errorMsg: 'Failed to update contact info',
	});

	let localName = $state('');
	let localEmail = $state('');
	let localPhone = $state('');

	$effect(() => {
		const s = settings;
		void saver.syncVersion;
		localName = (s?.contactInfo?.name as string) ?? '';
		localEmail = (s?.contactInfo?.email as string) ?? '';
		localPhone = (s?.contactInfo?.phone as string) ?? '';
	});

	let dirty = $derived(
		localName !== ((settings?.contactInfo?.name as string) ?? '') ||
			localEmail !== ((settings?.contactInfo?.email as string) ?? '') ||
			localPhone !== ((settings?.contactInfo?.phone as string) ?? '')
	);
</script>

{#await settingsData}
	<CardSkeleton />
{:then}
	<Card.Root>
		<Card.Header>
			<Card.Title>Contact Info</Card.Title>
			<Card.Description>Tenant administrator contact details</Card.Description>
		</Card.Header>
		<Card.Content>
			{#if settings}
				<div class="space-y-4">
					<div class="space-y-2">
						<Label for="contact-name">Name</Label>
						<Input id="contact-name" bind:value={localName} placeholder="Contact name" />
					</div>
					<div class="space-y-2">
						<Label for="contact-email">Email</Label>
						<Input
							id="contact-email"
							type="email"
							bind:value={localEmail}
							placeholder="admin@example.com"
						/>
					</div>
					<div class="space-y-2">
						<Label for="contact-phone">Phone</Label>
						<Input
							id="contact-phone"
							type="tel"
							bind:value={localPhone}
							placeholder="+1 (555) 000-0000"
						/>
					</div>
					<SaveButton
						{dirty}
						saving={saver.saving}
						onclick={() =>
							saver.run(async () => {
								if (!settingsData) return;
								await update_contact_info({
									tenant,
									body: { name: localName, email: localEmail, phone: localPhone },
								}).updates(settingsData);
							})}
					/>
				</div>
			{/if}
		</Card.Content>
	</Card.Root>
{/await}
