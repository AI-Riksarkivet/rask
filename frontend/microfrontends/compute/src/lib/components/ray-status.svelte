<script lang="ts">
	import { onMount } from 'svelte';
	import { getRayHealth } from '$lib/remote/compute.remote';

	// Ray cluster health — a live signal at the top of the compute overview. Lives in
	// the app (not @rask/ui) so the shared shell never imports app data; the read still
	// goes through the remote-query layer (getRayHealth) like every other compute read.
	const healthQuery = getRayHealth();
	const health = $derived(healthQuery.current ?? null);

	// POLL REASON: service liveness — the transition worth catching is a recovery, and nothing
	// publishes "the Ray head you could not reach is up again", so a timer is the only transport.
	// `.catch(() => {})` is mandatory: an uncaught refresh rejection (one 500) evicts the query
	// from cache and kills the loop.
	onMount(() => {
		const timer = setInterval(() => healthQuery.refresh().catch(() => {}), 5000);
		return () => clearInterval(timer);
	});
</script>

<div class="flex items-center gap-2 text-xs" title={health?.error ?? ''}>
	<span class="size-1.5 shrink-0 rounded-full {health?.ok ? 'bg-emerald-500' : 'bg-amber-500'}"
	></span>
	<span class="text-muted-foreground truncate">
		{#if health?.ok}
			Ray {health.ray_version ?? 'connected'}
		{:else}
			Ray offline
		{/if}
	</span>
</div>
