<script lang="ts">
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import {
		get_repl_collision,
		update_repl_collision,
		type ReplicationCollision,
	} from '$lib/remote/namespaces.remote.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	let replData = $derived(get_repl_collision({ tenant, name: namespaceName }));
	let repl = $derived((replData?.current ?? {}) as ReplicationCollision);

	const saver = useSave({
		successMsg: 'Replication collision settings updated',
		errorMsg: 'Failed to update replication collision settings',
	});

	let localAction = $state('');
	let localDeleteEnabled = $state(false);
	let localDeleteDays = $state('0');

	$effect(() => {
		const r = repl;
		void saver.syncVersion;
		localAction = r.action ?? '';
		localDeleteEnabled = r.deleteEnabled ?? false;
		localDeleteDays = String(r.deleteDays ?? 0);
	});

	let dirty = $derived(
		localAction !== (repl.action ?? '') ||
			localDeleteEnabled !== (repl.deleteEnabled ?? false) ||
			localDeleteDays !== String(repl.deleteDays ?? 0)
	);
</script>

<Card.Root class="flex h-full flex-col">
	<Card.Header>
		<Card.Title>Replication Collision</Card.Title>
		<Card.Description>
			How the system handles conflicts from simultaneous writes to replicated data.
		</Card.Description>
	</Card.Header>
	{#await replData}
		<Card.Content class="flex-1">
			<div class="flex flex-wrap gap-6">
				{#each Array(3) as _, i (i)}
					<div class="h-5 w-28 animate-pulse rounded bg-muted"></div>
				{/each}
			</div>
		</Card.Content>
	{:then}
		<Card.Content class="flex-1 space-y-4">
			<div class="space-y-1.5">
				<Label for="collision-action">Collision Action</Label>
				<select
					id="collision-action"
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
					bind:value={localAction}
				>
					<option value="MOVE">Move</option>
					<option value="RENAME">Rename</option>
				</select>
				<p class="text-xs text-muted-foreground">
					Move to .lost+found or rename with .collision suffix.
				</p>
			</div>
			<div class="flex items-center gap-2">
				<Switch id="auto-delete-collisions" bind:checked={localDeleteEnabled} />
				<Label for="auto-delete-collisions" class="text-sm">Auto-delete collisions</Label>
			</div>
			{#if localDeleteEnabled}
				<div class="space-y-1.5">
					<Label for="delete-days">Delete After (days)</Label>
					<Input id="delete-days" type="number" min="0" bind:value={localDeleteDays} />
					<p class="text-xs text-muted-foreground">0 = delete immediately, max 36,500.</p>
				</div>
			{/if}
		</Card.Content>
		<Card.Footer>
			<SaveButton
				{dirty}
				saving={saver.saving}
				onclick={() =>
					saver.run(async () => {
						if (!replData) return;
						await update_repl_collision({
							tenant,
							name: namespaceName,
							body: {
								action: localAction || undefined,
								deleteEnabled: localDeleteEnabled,
								deleteDays: parseInt(localDeleteDays, 10) || 0,
							},
						}).updates(replData);
					})}
			/>
		</Card.Footer>
	{/await}
</Card.Root>
