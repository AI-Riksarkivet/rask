<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import IpListEditor from '$lib/components/custom/ip-list-editor/ip-list-editor.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import { arraysEqual } from '$lib/utils/format.js';
	import {
		get_search_security,
		update_search_security,
		type SearchSecurity,
	} from '$lib/remote/tenant-info.remote.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	let securityData = $derived(get_search_security({ tenant }));
	let security = $derived((securityData?.current ?? {}) as SearchSecurity);

	const saver = useSave({
		successMsg: 'Search security updated',
		errorMsg: 'Failed to update search security',
	});

	let localAllowAddresses = $state<string[]>([]);
	let localDenyAddresses = $state<string[]>([]);
	let localAllowIfInBoth = $state(false);

	$effect(() => {
		const s = security;
		void saver.syncVersion;
		localAllowAddresses = [...(s.ipSettings?.allowAddresses ?? [])];
		localDenyAddresses = [...(s.ipSettings?.denyAddresses ?? [])];
		localAllowIfInBoth = s.ipSettings?.allowIfInBothLists ?? false;
	});

	let dirty = $derived(
		!arraysEqual(localAllowAddresses, security.ipSettings?.allowAddresses ?? []) ||
			!arraysEqual(localDenyAddresses, security.ipSettings?.denyAddresses ?? []) ||
			localAllowIfInBoth !== (security.ipSettings?.allowIfInBothLists ?? false)
	);
</script>

{#await securityData}
	<CardSkeleton />
{:then}
	<Card.Root>
		<Card.Header>
			<Card.Title>Search Security</Card.Title>
			<Card.Description>
				Controls which IP addresses can access the HCP metadata query API.
			</Card.Description>
		</Card.Header>
		<Card.Content class="space-y-6">
			<IpListEditor
				bind:addresses={localAllowAddresses}
				label="Allow List"
				emptyText="No allow list configured. All addresses are allowed by default."
			/>
			<IpListEditor
				bind:addresses={localDenyAddresses}
				label="Deny List"
				placeholder="IP address or CIDR (e.g. 192.168.1.0/24)"
				variant="destructive"
				emptyText="No deny list configured."
			/>

			<div class="flex items-center gap-2">
				<Switch id="allow-if-both" bind:checked={localAllowIfInBoth} />
				<Label for="allow-if-both" class="text-sm">Allow if in both lists</Label>
			</div>
			<p class="text-xs text-muted-foreground">
				When enabled, addresses that appear in both allow and deny lists will be allowed.
			</p>
		</Card.Content>
		<Card.Footer>
			<SaveButton
				{dirty}
				saving={saver.saving}
				onclick={() =>
					saver.run(async () => {
						if (!securityData) return;
						await update_search_security({
							tenant,
							body: {
								ipSettings: {
									allowAddresses: localAllowAddresses.filter(Boolean),
									denyAddresses: localDenyAddresses.filter(Boolean),
									allowIfInBothLists: localAllowIfInBoth,
								},
							},
						}).updates(securityData);
					})}
			/>
		</Card.Footer>
	</Card.Root>
{/await}
