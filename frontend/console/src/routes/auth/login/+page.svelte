<script lang="ts">
	// `/auth/login?redirect=<path>` — the shell's sign-in link: straight to the issuer, back to <path>.
	// On a stack that signs nobody in it is a no-op back to the landing, never a dead end.
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { auth, initAuth, login } from '$lib/auth.svelte';

	onMount(async () => {
		await initAuth();
		if (!auth.enabled) {
			await goto('/', { replaceState: true });
			return;
		}
		const target = page.url.searchParams.get('redirect') ?? '/';
		await login(target.startsWith('/') && !target.startsWith('//') ? target : '/');
	});
</script>

<svelte:head><title>Sign in · rask</title></svelte:head>

<main class="text-muted-foreground p-8 text-sm">Redirecting to sign in…</main>
