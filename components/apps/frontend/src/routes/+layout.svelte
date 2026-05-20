<script lang="ts">
	import '../app.css';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();
</script>

<ModeWatcher defaultMode="dark" />
<Toaster />

<div class="flex h-screen flex-col overflow-hidden relative z-[1]">
	{@render children()}
</div>
