<script lang="ts">
	// `/auth/logout` — the shell's sign-out link: forget the reader's activity log, drop the in-memory
	// session, end the issuer's.
	import { onMount } from 'svelte';
	import { activity } from '#lib/activity.svelte.ts';
	import { logout } from '#lib/auth.svelte.ts';

	onMount(() => {
		activity.forget();
		void logout();
	});
</script>

<svelte:head><title>Signing out · rask</title></svelte:head>

<main class="text-muted-foreground p-8 text-sm">Signing out…</main>
