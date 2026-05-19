<script lang="ts">
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { toast } from 'svelte-sonner';
	import FormDialog from '$lib/components/custom/form-dialog/form-dialog.svelte';
	import { change_password } from '$lib/remote/users.remote.js';

	let {
		tenant,
		username,
		open = $bindable(false),
	}: {
		tenant: string;
		username: string;
		open: boolean;
	} = $props();

	let newPassword = $state('');
	let confirmPassword = $state('');
	let changingPassword = $state(false);
	let error = $state('');

	let passwordValid = $derived(newPassword.length > 0 && newPassword === confirmPassword);

	async function handleChangePassword(e: SubmitEvent) {
		e.preventDefault();
		if (!passwordValid) return;
		changingPassword = true;
		error = '';
		try {
			await change_password({ tenant, username, password: newPassword });
			toast.success('Password changed successfully');
			newPassword = '';
			confirmPassword = '';
			open = false;
		} catch {
			error = 'Failed to change password';
		} finally {
			changingPassword = false;
		}
	}
</script>

<FormDialog
	bind:open
	title="Change Password"
	description={`Set a new password for "${username}".`}
	submitLabel="Change Password"
	loading={changingPassword}
	{error}
	onsubmit={handleChangePassword}
	class="sm:max-w-md"
>
	<div class="space-y-2">
		<Label for="new-password">New Password</Label>
		<Input
			id="new-password"
			type="password"
			bind:value={newPassword}
			placeholder="Enter new password"
		/>
	</div>
	<div class="space-y-2">
		<Label for="confirm-password">Confirm Password</Label>
		<Input
			id="confirm-password"
			type="password"
			bind:value={confirmPassword}
			placeholder="Confirm new password"
		/>
	</div>
	{#if newPassword.length > 0 && confirmPassword.length > 0 && newPassword !== confirmPassword}
		<p class="text-sm text-destructive">Passwords do not match.</p>
	{/if}
</FormDialog>
