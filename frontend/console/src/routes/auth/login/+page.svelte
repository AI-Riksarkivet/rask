<script lang="ts">
	import { resolve } from '$app/paths';
	// `/auth/login?redirect=<path>` — the shell's sign-in link: straight to the issuer, back to <path>.
	// `&prompt=login` (sign-out against an issuer with no end-session endpoint) makes an issuer that
	// keeps a session authenticate the person again rather than answer from it.
	// On a stack that signs nobody in it is a no-op back to the landing, never a dead end.
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { auth, initAuth, login, REAUTHENTICATE } from '#lib/auth.svelte.ts';
	import { sameOriginPath } from '#lib/return-path.ts';

	onMount(async () => {
		await initAuth();
		if (!auth.enabled) {
			await goto(resolve('/'), { replace: true });
			return;
		}
		const target = page.url.searchParams.get('redirect') ?? '/';
		const reauthenticate = page.url.searchParams.get(REAUTHENTICATE.param) === REAUTHENTICATE.value;
		await login(sameOriginPath(target), { reauthenticate });
	});
</script>

<svelte:head><title>Sign in · rask</title></svelte:head>

<main class="text-muted-foreground p-8 text-sm">Redirecting to sign in…</main>
