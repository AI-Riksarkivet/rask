<script lang="ts">
	import '../app.css';
	import { onNavigate } from '$app/navigation';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from '$lib/components/ui/sonner/index.js';
	import { browser } from '$app/environment';
	let { children } = $props();

	onNavigate((navigation) => {
		if (!document.startViewTransition) return;
		const from = navigation.from?.url.pathname ?? '';
		const to = navigation.to?.url.pathname ?? '';
		const isLoginTransition = from === '/login' || to === '/login';
		if (!isLoginTransition) return;
		return new Promise((resolve) => {
			document.startViewTransition(async () => {
				resolve();
				await navigation.complete;
			});
		});
	});
</script>

<ModeWatcher />
{@render children()}
{#if browser}
	<Toaster />
{/if}
