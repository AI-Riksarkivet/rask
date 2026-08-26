<script lang="ts">
	import { liveRead } from '@rask/api/live';
	import { rayClock } from '$lib/live/ray-clock.svelte';
	import { getRayHealth } from '$lib/remote/compute.remote';

	// Ray cluster health — a live signal at the top of the compute overview. Lives in
	// the app (not @rask/ui) so the shared shell never imports app data; the read still
	// goes through the remote-query layer (getRayHealth) like every other compute read.
	const healthQuery = getRayHealth();
	const health = $derived(healthQuery.current ?? null);

	// Liveness, on the zone's ONE clock. The reason a clock is right here is unchanged and still the
	// strongest case in the zone: the transition worth catching is a RECOVERY, and by construction
	// nothing publishes "the Ray head you could not reach is up again" — so the only signal is to try
	// again. What changed is that it no longer owns a private interval to do it; `$lib/live/ray-clock`
	// holds the single one and its POLL REASON.
	$effect(() => rayClock.subscribe());
	liveRead(
		() => rayClock.cursor,
		() => {
			rayClock.refresh('health', healthQuery);
		},
	);
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
