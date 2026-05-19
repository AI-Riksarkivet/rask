<script lang="ts">
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { HelpCircle } from 'lucide-svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import {
		get_namespace,
		update_namespace,
		update_versioning,
		type Namespace,
	} from '$lib/remote/namespaces.remote.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	let nsData = $derived(get_namespace({ tenant, name: namespaceName }));
	let ns = $derived((nsData?.current ?? null) as Namespace | null);

	const saver = useSave({
		successMsg: 'Settings updated',
		errorMsg: 'Failed to update settings',
	});

	let localSearchEnabled = $state(false);
	let localVersioningEnabled = $state(false);

	$effect(() => {
		const n = ns;
		void saver.syncVersion;
		localSearchEnabled = n?.searchEnabled ?? false;
		localVersioningEnabled = n?.versioningSettings?.enabled ?? false;
	});

	let dirty = $derived(
		localSearchEnabled !== (ns?.searchEnabled ?? false) ||
			localVersioningEnabled !== (ns?.versioningSettings?.enabled ?? false)
	);
</script>

{#snippet featureSwitch(
	id: string,
	label: string,
	checked: boolean,
	onChange: (v: boolean) => void,
	desc: string
)}
	<div class="flex items-center gap-2 text-sm">
		<Switch {id} {checked} onCheckedChange={onChange} />
		<Label for={id}>{label}</Label>
		<Tooltip.Root>
			<Tooltip.Trigger>
				{#snippet child({ props })}
					<span {...props}>
						<HelpCircle class="h-3 w-3 text-muted-foreground" />
					</span>
				{/snippet}
			</Tooltip.Trigger>
			<Tooltip.Content side="right">{desc}</Tooltip.Content>
		</Tooltip.Root>
	</div>
{/snippet}

<Card.Root class="flex h-full flex-col">
	<Card.Header class="pb-3">
		<Card.Title class="text-base">Features</Card.Title>
		<Card.Description>
			Toggle namespace-level capabilities like full-text search and object versioning.
		</Card.Description>
	</Card.Header>
	<Card.Content>
		<div class="flex flex-wrap gap-x-6 gap-y-3">
			{@render featureSwitch(
				'feat-search',
				'Search',
				localSearchEnabled,
				(v) => (localSearchEnabled = v),
				'Enable metadata query engine indexing'
			)}
			{@render featureSwitch(
				'feat-versioning',
				'Versioning',
				localVersioningEnabled,
				(v) => (localVersioningEnabled = v),
				'Keep previous versions of objects on update or delete'
			)}
		</div>
	</Card.Content>
	<Card.Footer>
		<SaveButton
			{dirty}
			saving={saver.saving}
			onclick={() =>
				saver.run(async () => {
					if (!nsData) return;
					if (localSearchEnabled !== (ns?.searchEnabled ?? false)) {
						await update_namespace({
							tenant,
							name: namespaceName,
							body: { searchEnabled: localSearchEnabled },
						});
					}
					if (localVersioningEnabled !== (ns?.versioningSettings?.enabled ?? false)) {
						await update_versioning({
							tenant,
							name: namespaceName,
							enabled: localVersioningEnabled,
						});
					}
					nsData.refresh();
				})}
		/>
	</Card.Footer>
</Card.Root>
