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
<!-- Toaster touches browser-only APIs; render it client-side only to keep SSR clean. -->
{#if browser}
	<Toaster />
{/if}

<!-- ONE shared sidebar from @rask/ui — identical across every microfrontend, no drift. -->
<AppShell pathname={page.url.pathname}>
	{@render children()}
</AppShell>
