<script lang="ts">
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { HelpCircle } from 'lucide-svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import { get_group, update_group } from '$lib/remote/users.remote.js';
	import { GROUP_ROLES, ROLE_DESCRIPTIONS, getGroupName, getGroupRoles } from '$lib/constants.js';
	import type { GroupAccount } from '$lib/constants.js';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';

	let {
		tenant,
		groupname,
	}: {
		tenant: string;
		groupname: string;
	} = $props();

	let groupData = $derived(get_group({ tenant, groupname }));
	let group = $derived((groupData?.current ?? null) as GroupAccount | null);

	const saver = useSave({
		successMsg: 'Group updated successfully',
		errorMsg: 'Failed to update group',
	});

	let localDescription = $state('');
	let localRoles = $state<string[]>([]);

	$effect(() => {
		const g = group;
		void saver.syncVersion;
		localDescription = g?.description ?? '';
		localRoles = g ? getGroupRoles(g) : [];
	});

	let dirty = $derived(
		localDescription !== (group?.description ?? '') ||
			JSON.stringify([...localRoles].sort()) !==
				JSON.stringify([...(group ? getGroupRoles(group) : [])].sort())
	);

	function toggleRole(role: string) {
		if (localRoles.includes(role)) {
			localRoles = localRoles.filter((r) => r !== role);
		} else {
			localRoles = [...localRoles, role];
		}
	}
</script>

{#await groupData}
	<div class="rounded-lg border p-5">
		<div class="grid gap-3 sm:grid-cols-2">
			{#each Array(3) as _, i (i)}
				<div class="space-y-1">
					<div class="h-3 w-20 animate-pulse rounded bg-muted"></div>
					<div class="h-8 w-full animate-pulse rounded bg-muted"></div>
				</div>
			{/each}
		</div>
	</div>
{:then}
	{#if !group}
		<div class="rounded-lg border border-dashed p-8 text-center">
			<p class="text-muted-foreground">Group not found or could not be loaded.</p>
		</div>
	{:else}
		<div class="rounded-lg border p-5">
			<h3 class="text-sm font-semibold uppercase tracking-wide text-muted-foreground">General</h3>
			<div class="mt-3 grid gap-x-4 gap-y-2 sm:grid-cols-2">
				<div class="space-y-1">
					<Label class="text-xs">Group Name</Label>
					<p class="text-sm font-medium">{getGroupName(group)}</p>
				</div>
				<div class="space-y-1">
					<Label for="group-description" class="text-xs">Description</Label>
					<Input
						id="group-description"
						bind:value={localDescription}
						placeholder="Optional description"
						class="h-8 text-sm"
					/>
				</div>
			</div>

			<div class="mt-4 border-t pt-3">
				<h3 class="text-sm font-semibold uppercase tracking-wide text-muted-foreground">Roles</h3>
				<div class="mt-2 flex flex-wrap gap-x-6 gap-y-2">
					{#each GROUP_ROLES as role (role)}
						<label class="flex items-center gap-1.5 text-sm">
							<Checkbox
								checked={localRoles.includes(role)}
								onCheckedChange={() => toggleRole(role)}
							/>
							{role}
							{#if ROLE_DESCRIPTIONS[role]}
								<Tooltip.Root>
									<Tooltip.Trigger>
										{#snippet child({ props })}
											<span {...props}><HelpCircle class="h-3 w-3 text-muted-foreground" /></span>
										{/snippet}
									</Tooltip.Trigger>
									<Tooltip.Content>{ROLE_DESCRIPTIONS[role]}</Tooltip.Content>
								</Tooltip.Root>
							{/if}
						</label>
					{/each}
				</div>
				<div class="pt-4">
					<SaveButton
						{dirty}
						saving={saver.saving}
						onclick={() =>
							saver.run(async () => {
								if (!groupData) return;
								const body: Record<string, unknown> = {
									description: localDescription,
									roles: { role: localRoles },
								};
								await update_group({ tenant, groupname, body }).updates(groupData);
							})}
					/>
				</div>
			</div>
		</div>
	{/if}
{/await}
