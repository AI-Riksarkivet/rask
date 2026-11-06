<script lang="ts">
	import { match, resolve } from '$app/paths';
	// `/auth/callback` — where the issuer sends the browser back with a code. The code is exchanged
	// here, with the PKCE verifier this browser kept, and the person lands on the page they asked for.
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { completeLogin } from '#lib/auth.svelte.ts';

	let failure = $state<string | null>(null);

	onMount(async () => {
		try {
			// The return path rode the OIDC state from `/auth/login?redirect=`; `completeLogin` keeps it
			// only when it is a same-origin path. `goto` rejects a URL that names no route, so a return
			// path that names none lands on the estate home rather than failing a completed sign-in.
			const returnTo = await completeLogin();
			const known = (await match(new URL(returnTo, location.origin))) !== null;
			// eslint-disable-next-line svelte/no-navigation-without-resolve -- a return path read from the OIDC state, already a resolved pathname
			await goto(known ? returnTo : resolve('/'), { replace: true, refreshAll: true });
		} catch (err) {
			failure = err instanceof Error ? err.message : String(err);
		}
	});
</script>

<svelte:head><title>Signing in · rask</title></svelte:head>

<main class="p-8 text-sm">
	{#if failure}
		<p class="text-destructive">Sign-in did not complete: {failure}</p>
		<p class="mt-2"><a class="underline" href={resolve('/auth/login')}>Try again</a></p>
	{:else}
		<p class="text-muted-foreground">Signing in…</p>
	{/if}
</main>
