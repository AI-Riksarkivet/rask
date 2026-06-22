<script lang="ts">
	import '../app.css';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();
</script>

<div class="flex h-svh flex-col overflow-hidden">
	<header class="flex h-12 shrink-0 items-center gap-2 border-b px-4 text-sm">
		<span class="font-semibold tracking-tight">RASK</span>
		<span class="text-muted-foreground">/ Storage</span>
		<span class="text-muted-foreground ml-2 text-xs">microfrontend · base /storage</span>
	</header>
	<main class="flex-1 overflow-auto">
		{@render children()}
	</main>
</div>
