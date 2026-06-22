<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@rask/ui/shell';
	import RayStatus from '$lib/components/ray-status.svelte';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();
</script>

<ModeWatcher defaultMode="dark" />
{#if browser}
	<Toaster />
{/if}

{#snippet status()}
	<RayStatus />
{/snippet}

<!-- Shared shell from @rask/ui — identical chrome across every microfrontend.
     `base` (=/compute) strips the breadcrumb prefix; `status` renders Ray health
     in the footer profile dropdown. -->
<AppShell pathname={page.url.pathname} {base} {status}>
	{@render children()}
</AppShell>
