<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import { AppShell } from '@rask/ui/shell';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();
</script>

<!-- The shared AppShell (one grouped sidebar) from @rask/ui — identical to the
     monolith's, zero drift. pathname comes from $app/state (the lib can't read it). -->
<AppShell pathname={page.url.pathname}>
	{@render children()}
</AppShell>
