<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();
</script>

<ModeWatcher defaultMode="dark" />
<!-- Toaster touches browser-only APIs; render it client-side only to keep SSR clean. -->
{#if browser}
	<Toaster />
{/if}

<!-- Global providers only. The home picker (`/`) renders bare (no sidebar); the
     in-project chrome (shared @rask/ui sidebar + breadcrumb) lives in
     [project]/+layout.svelte — you only see the sidebar once you're inside a project. -->
{@render children()}
