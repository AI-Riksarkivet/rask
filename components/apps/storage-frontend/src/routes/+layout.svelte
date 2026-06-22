<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();
</script>

<!-- ModeWatcher sets `class="dark"` on <html> so the .dark theme tokens apply —
     without it the shared sidebar renders unstyled. Identical wiring to every
     other microfrontend (compute/monolith). -->
<ModeWatcher defaultMode="dark" />
{#if browser}
	<Toaster />
{/if}

<!-- The shared AppShell (one grouped sidebar) from @rask/ui — identical to every
     other microfrontend, zero drift. `base` (=/storage) strips the breadcrumb prefix. -->
<AppShell pathname={page.url.pathname} {base}>
	{@render children()}
</AppShell>
