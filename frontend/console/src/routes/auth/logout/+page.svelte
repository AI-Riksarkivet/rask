<script lang="ts">
	// `/auth/logout` — the shell's sign-out link: drop the in-memory session, end the issuer's.
	import { onMount } from 'svelte';
	import { logout } from '$lib/auth.svelte';

	onMount(() => {
		void logout();
	});
</script>

<svelte:head><title>Signing out · rask</title></svelte:head>

<main class="text-muted-foreground p-8 text-sm">Signing out…</main>
