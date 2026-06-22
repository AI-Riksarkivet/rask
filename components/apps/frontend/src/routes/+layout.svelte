<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import * as Sidebar from '$lib/components/ui/sidebar';
	import AppSidebar from '$lib/components/layout/app-sidebar.svelte';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();
</script>

<ModeWatcher defaultMode="dark" />
<!-- Toaster touches browser-only APIs; render it client-side only to keep SSR clean. -->
{#if browser}
	<Toaster />
{/if}

<!-- ONE sidebar for the whole app (replaces the old per-page ray-shell icon-rail).
     h-svh + overflow-hidden so the inner panes own their scrolling and the viewer
     canvas can fill the viewport. -->
<Sidebar.Provider class="h-svh overflow-hidden">
	<AppSidebar />
	<Sidebar.Inset class="overflow-hidden">
		{@render children()}
	</Sidebar.Inset>
</Sidebar.Provider>
