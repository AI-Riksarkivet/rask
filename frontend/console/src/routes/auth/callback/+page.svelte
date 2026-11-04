<script lang="ts">
	// `/auth/callback` — where the issuer sends the browser back with a code. The code is exchanged
	// here, with the PKCE verifier this browser kept, and the person lands on the page they asked for.
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { completeLogin } from '$lib/auth.svelte';

	let failure = $state<string | null>(null);

	onMount(async () => {
		try {
			await goto(await completeLogin(), { replaceState: true, invalidateAll: true });
		} catch (err) {
			failure = err instanceof Error ? err.message : String(err);
		}
	});
</script>

<svelte:head><title>Signing in · rask</title></svelte:head>

<main class="p-8 text-sm">
	{#if failure}
		<p class="text-destructive">Sign-in did not complete: {failure}</p>
		<p class="mt-2"><a class="underline" href="/auth/login">Try again</a></p>
	{:else}
		<p class="text-muted-foreground">Signing in…</p>
	{/if}
</main>
