<script lang="ts">
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { HelpCircle, Copy, Check } from 'lucide-svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import { useCopyFeedback } from '$lib/utils/use-copy-feedback.svelte.js';
	import { get_user, update_user } from '$lib/remote/users.remote.js';
	import { AVAILABLE_ROLES, ROLE_DESCRIPTIONS, getUserRoles } from '$lib/constants.js';
	import type { User } from '$lib/constants.js';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';

	let {
		tenant,
		username,
	}: {
		tenant: string;
		username: string;
	} = $props();

	let userData = $derived(get_user({ tenant, username }));
	let user = $derived((userData?.current ?? null) as User | null);

	const saver = useSave({
		successMsg: 'User updated successfully',
		errorMsg: 'Failed to update user',
	});

	let localFullName = $state('');
	let localDescription = $state('');
	let localEnabled = $state(true);
	let localRoles = $state<string[]>([]);

	$effect(() => {
		const u = user;
		void saver.syncVersion;
		localFullName = u?.fullName ?? '';
		localDescription = u?.description ?? '';
		localEnabled = u?.enabled ?? true;
		localRoles = u ? getUserRoles(u) : [];
	});

	let dirty = $derived(
		localFullName !== (user?.fullName ?? '') ||
			localDescription !== (user?.description ?? '') ||
			localEnabled !== (user?.enabled ?? true) ||
			JSON.stringify([...localRoles].sort()) !==
				JSON.stringify([...(user ? getUserRoles(user) : [])].sort())
	);

	function toggleRole(role: string) {
		if (localRoles.includes(role)) {
			localRoles = localRoles.filter((r) => r !== role);
		} else {
			localRoles = [...localRoles, role];
		}
	}

	// Copy helper
	const guidCopy = useCopyFeedback();
</script>

{#await userData}
	<div class="rounded-lg border p-5">
		<div class="grid gap-3 sm:grid-cols-2">
			{#each Array(4) as _, i (i)}
				<div class="space-y-1">
					<div class="h-3 w-20 animate-pulse rounded bg-muted"></div>
					<div class="h-8 w-full animate-pulse rounded bg-muted"></div>
				</div>
			{/each}
		</div>
	</div>
{:then}
	{#if !user}
		<div class="rounded-lg border border-dashed p-8 text-center">
			<p class="text-muted-foreground">User not found or could not be loaded.</p>
		</div>
	{:else}
		<div class="rounded-lg border p-5">
			<h3 class="text-sm font-semibold uppercase tracking-wide text-muted-foreground">General</h3>
			<div class="mt-3 grid gap-x-4 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
				<div class="space-y-1">
					<Label class="text-xs">Username</Label>
					<p class="text-sm font-medium">{user.username}</p>
				</div>
				<div class="space-y-1">
					<Label class="text-xs">Canonical ID (userGUID)</Label>
					{#if user.userGUID}
						<div class="flex items-center gap-1">
							<p class="truncate font-mono text-xs text-muted-foreground">{user.userGUID}</p>
							<Tooltip.Root>
								<Tooltip.Trigger>
									{#snippet child({ props })}
										<Button
											variant="ghost"
											size="icon"
											class="h-6 w-6 shrink-0"
											onclick={() => guidCopy.copy(user.userGUID!)}
											{...props}
										>
											{#if guidCopy.copied}<Check class="h-3 w-3 text-emerald-500" />{:else}<Copy
													class="h-3 w-3"
												/>{/if}
										</Button>
									{/snippet}
								</Tooltip.Trigger>
								<Tooltip.Content
									>{guidCopy.copied ? 'Copied!' : 'Copy canonical ID'}</Tooltip.Content
								>
							</Tooltip.Root>
						</div>
					{:else}
						<p class="text-xs text-muted-foreground">Not available</p>
					{/if}
				</div>
				<div class="space-y-1">
					<Label for="user-fullname" class="text-xs">Full Name</Label>
					<Input
						id="user-fullname"
						bind:value={localFullName}
						placeholder="Full name"
						class="h-8 text-sm"
					/>
				</div>
				<div class="space-y-1">
					<Label for="user-description" class="text-xs">Description</Label>
					<Input
						id="user-description"
						bind:value={localDescription}
						placeholder="Optional"
						class="h-8 text-sm"
					/>
				</div>
				<div class="flex items-end pb-1">
					<label class="flex items-center gap-2 text-sm">
						<Checkbox bind:checked={localEnabled} />
						Enabled
					</label>
				</div>
			</div>

			<div class="mt-4 border-t pt-3">
				<h3 class="text-sm font-semibold uppercase tracking-wide text-muted-foreground">Roles</h3>
				<div class="mt-2 flex flex-wrap gap-x-6 gap-y-2">
					{#each AVAILABLE_ROLES as role (role)}
						<label class="flex items-center gap-1.5 text-sm">
							<Checkbox
								checked={localRoles.includes(role)}
								onCheckedChange={() => toggleRole(role)}
							/>
							{role}
							<Tooltip.Root>
								<Tooltip.Trigger>
									{#snippet child({ props })}
										<span {...props}><HelpCircle class="h-3 w-3 text-muted-foreground" /></span>
									{/snippet}
								</Tooltip.Trigger>
								<Tooltip.Content>{ROLE_DESCRIPTIONS[role]}</Tooltip.Content>
							</Tooltip.Root>
						</label>
					{/each}
				</div>
				<div class="pt-4">
					<SaveButton
						{dirty}
						saving={saver.saving}
						onclick={() =>
							saver.run(async () => {
								if (!userData) return;
								const body: Record<string, unknown> = {
									fullName: localFullName,
									description: localDescription,
									enabled: localEnabled,
									roles: { role: localRoles },
								};
								await update_user({ tenant, username, body }).updates(userData);
							})}
					/>
				</div>
			</div>
		</div>
	{/if}
{/await}
