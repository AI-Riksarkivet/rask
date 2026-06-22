<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();
</script>

<ModeWatcher defaultMode="dark" />
{#if browser}
	<Toaster />
{/if}

<!-- Shared shell from @rask/ui — identical chrome across every microfrontend.
     Ray health is a live signal at the top of the compute overview, not in the shell. -->
<AppShell pathname={page.url.pathname}>
	{@render children()}
</AppShell>
