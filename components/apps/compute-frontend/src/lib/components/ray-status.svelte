<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { rayHealth } from '@rask/api';

	// Ray cluster health — a live signal at the top of the compute overview. Lives in
	// the app (not @rask/ui) so the shared shell never imports app data like @rask/api.
	let health = $state<{ ok: boolean; version?: string; error?: string } | null>(null);
	let timer: ReturnType<typeof setInterval> | null = null;

	async function refresh() {
		try {
			const r = await rayHealth();
			health = { ok: r.ok, version: r.ray_version, error: r.error };
		} catch (e) {
			health = { ok: false, error: e instanceof Error ? e.message : String(e) };
		}
	}

	onMount(() => {
		refresh();
		timer = setInterval(refresh, 5000);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<div class="flex items-center gap-2 text-xs" title={health?.error ?? ''}>
	<span class="size-1.5 shrink-0 rounded-full {health?.ok ? 'bg-emerald-500' : 'bg-amber-500'}"
	></span>
	<span class="text-muted-foreground truncate">
		{#if health?.ok}
			Ray {health.version ?? 'connected'}
		{:else}
			Ray offline
		{/if}
	</span>
</div>
