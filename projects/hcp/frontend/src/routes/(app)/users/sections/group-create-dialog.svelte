<script lang="ts">
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { toast } from 'svelte-sonner';
	import FormDialog from '$lib/components/custom/form-dialog/form-dialog.svelte';
	import { get_groups, create_group } from '$lib/remote/users.remote.js';
	import { GROUP_ROLES } from '$lib/constants.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		tenant,
		open = $bindable(false),
	}: {
		tenant: string;
		open: boolean;
	} = $props();

	let groupsData = $derived(get_groups({ tenant }));
	let createError = $state('');
	let creating = $state(false);

	async function handleCreateGroup(e: SubmitEvent) {
		e.preventDefault();
		if (!groupsData) return;
		const form = e.currentTarget as HTMLFormElement;
		const fd = new FormData(form);
		const groupname = fd.get('groupname') as string;
		if (!groupname) return;
		creating = true;
		createError = '';
		try {
			const description = (fd.get('description') as string) || undefined;
			const roles = GROUP_ROLES.filter((r) => fd.has(`role-${r}`));
			await create_group({ tenant, groupname, description, roles }).updates(groupsData);
			toast.success(`Group "${groupname}" created`);
			open = false;
			form.reset();
		} catch (err) {
			createError = getErrorMessage(err, 'Failed to create group');
		} finally {
			creating = false;
		}
	}
</script>

<FormDialog
	bind:open
	title="Create Group"
	description="Add a new group to this tenant."
	loading={creating}
	error={createError}
	onsubmit={handleCreateGroup}
>
	<div class="space-y-2">
		<Label for="cg-name">Group Name</Label>
		<Input id="cg-name" name="groupname" placeholder="my-group" required />
	</div>
	<div class="space-y-2">
		<Label for="cg-desc">Description</Label>
		<Input id="cg-desc" name="description" placeholder="Optional description" />
	</div>
	<div class="space-y-2">
		<Label>Roles</Label>
		<div class="flex flex-wrap gap-4">
			{#each GROUP_ROLES as role (role)}
				<label class="flex items-center gap-2 text-sm">
					<Checkbox name="role-{role}" />
					{role}
				</label>
			{/each}
		</div>
	</div>
</FormDialog>
