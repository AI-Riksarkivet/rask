<script lang="ts">
	import { page } from '$app/state';
	import { KeyRound, Info } from 'lucide-svelte';
	import CopyableInput from '$lib/components/custom/copyable-input/copyable-input.svelte';
	import { get_s3_credentials } from '$lib/remote/buckets.remote.js';

	let {
		tenant,
		username,
	}: {
		tenant: string;
		username: string;
	} = $props();

	let loggedInUsername = $derived(page.data.username as string);
	let isOwnAccount = $derived(username === loggedInUsername);
	let credsData = $derived(isOwnAccount ? get_s3_credentials() : undefined);
	let creds = $derived(
		credsData?.current as
			| { access_key_id: string; secret_access_key: string; username: string; endpoint_url: string }
			| undefined
	);
</script>

<div class="rounded-lg border p-5">
	<div class="flex items-center gap-2">
		<KeyRound class="h-4 w-4 text-muted-foreground" />
		<h3 class="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
			S3 Credentials
		</h3>
	</div>
	{#if !isOwnAccount}
		<div class="mt-3 flex items-start gap-2 text-sm text-muted-foreground">
			<Info class="mt-0.5 h-4 w-4 shrink-0" />
			<p>
				S3 credentials can only be viewed for your own account. You are viewing <strong
					>{username}</strong
				>.
			</p>
		</div>
	{:else}
		{#await credsData}
			<div class="mt-3 space-y-3">
				{#each Array(3) as _, i (i)}
					<div class="space-y-1">
						<div class="h-3 w-20 animate-pulse rounded bg-muted"></div>
						<div class="h-8 w-full animate-pulse rounded bg-muted"></div>
					</div>
				{/each}
			</div>
		{:then}
			{#if creds && creds.access_key_id}
				<div class="mt-3 space-y-3">
					<CopyableInput label="Access Key ID" value={creds.access_key_id} />
					<CopyableInput label="Secret Access Key" value={creds.secret_access_key} secret />
					{#if creds.endpoint_url}
						<CopyableInput label="S3 Endpoint URL" value={creds.endpoint_url} />
					{/if}
				</div>
			{:else}
				<p class="mt-3 text-center text-sm text-muted-foreground">Could not load S3 credentials.</p>
			{/if}
		{/await}
	{/if}
</div>
