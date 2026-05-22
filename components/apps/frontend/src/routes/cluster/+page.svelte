<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { rayCluster, type RayClusterPayload } from '$lib/api';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Card } from '$lib/components/ui/card';
	import { Badge } from '$lib/components/ui/badge';

	let payload = $state<RayClusterPayload | null>(null);
	let error = $state<string | null>(null);
	let timer: ReturnType<typeof setInterval> | null = null;

	async function refresh() {
		try {
			payload = await rayCluster();
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	onMount(() => {
		refresh();
		timer = setInterval(refresh, 5000);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	function bytesGb(b: number): number {
		return b / 1024 ** 3;
	}

	function pct(used: number, total: number): number {
		if (!total) return 0;
		return Math.min(100, (used / total) * 100);
	}
</script>

<svelte:head>
	<title>Cluster — RASK</title>
</svelte:head>

<RayShell title="Cluster">
	<div class="flex flex-col gap-4 p-6 text-sm">
		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 p-3 text-destructive">{error}</Card>
		{/if}

		{#if payload && !payload.ok}
			<Card class="border-amber-500/40 bg-amber-500/10 p-3">
				Ray dashboard unreachable at <span class="font-mono">{payload.dashboard_url}</span>
				{#if payload.error}
					<div class="mt-1 text-xs text-muted-foreground">{payload.error}</div>
				{/if}
			</Card>
		{/if}

		{#if payload?.ok && payload.total_resources}
			{@const tr = payload.total_resources}
			{@const ur = payload.used_resources!}

			<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
				<Card class="p-4">
					<div class="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
						Nodes
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">
						{payload.alive_count}/{payload.node_count}
					</div>
					<div class="text-xs text-muted-foreground">alive / total</div>
				</Card>
				<Card class="p-4">
					<div class="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
						GPU
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">
						{ur.GPU.toFixed(1)}<span class="text-base text-muted-foreground"
							>/{tr.GPU.toFixed(0)}</span
						>
					</div>
					<div class="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
						<div class="h-full bg-emerald-500 transition-all" style:width={`${pct(ur.GPU, tr.GPU)}%`}></div>
					</div>
				</Card>
				<Card class="p-4">
					<div class="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
						CPU
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">
						{ur.CPU.toFixed(0)}<span class="text-base text-muted-foreground"
							>/{tr.CPU.toFixed(0)}</span
						>
					</div>
					<div class="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
						<div class="h-full bg-sky-500 transition-all" style:width={`${pct(ur.CPU, tr.CPU)}%`}></div>
					</div>
				</Card>
				<Card class="p-4">
					<div class="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
						Memory
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">
						{bytesGb(ur.memory).toFixed(0)}<span class="text-base text-muted-foreground"
							>/{bytesGb(tr.memory).toFixed(0)} GiB</span
						>
					</div>
					<div class="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
						<div
							class="h-full bg-violet-500 transition-all"
							style:width={`${pct(ur.memory, tr.memory)}%`}
						></div>
					</div>
				</Card>
			</section>

			<Card class="overflow-hidden">
				<div class="border-b px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
					Nodes
				</div>
				<div class="max-h-[60vh] overflow-auto">
					<table class="w-full border-collapse text-xs">
						<thead class="sticky top-0 z-10 bg-card text-left">
							<tr class="border-b">
								{#each ['state', 'node_id', 'ip', 'GPU', 'CPU', 'memory'] as h (h)}
									<th class="px-3 py-2 font-medium text-muted-foreground">{h}</th>
								{/each}
							</tr>
						</thead>
						<tbody>
							{#each payload.nodes ?? [] as n (n.node_id)}
								{@const gpuT = n.resources_total.GPU ?? 0}
								{@const gpuU = n.resources_used.GPU ?? 0}
								{@const cpuT = n.resources_total.CPU ?? 0}
								{@const cpuU = n.resources_used.CPU ?? 0}
								{@const memT = n.resources_total.memory ?? 0}
								{@const memU = n.resources_used.memory ?? 0}
								<tr class="border-b border-border/40 hover:bg-muted/40">
									<td class="px-3 py-1.5">
										{#if n.alive}
											<Badge variant="success">alive</Badge>
										{:else}
											<Badge variant="destructive">dead</Badge>
										{/if}
									</td>
									<td class="px-3 py-1.5 font-mono text-[11px]">{n.node_id?.slice(0, 12) ?? '—'}</td>
									<td class="px-3 py-1.5 font-mono">{n.node_ip ?? '—'}</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{gpuU.toFixed(1)}/{gpuT.toFixed(0)}
									</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{cpuU.toFixed(0)}/{cpuT.toFixed(0)}
									</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{bytesGb(memU).toFixed(0)}/{bytesGb(memT).toFixed(0)} GiB
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</Card>

			<div class="text-xs text-muted-foreground">
				dashboard: <a
					class="text-primary hover:underline"
					href={payload.dashboard_url}
					target="_blank"
					rel="noopener">{payload.dashboard_url} ↗</a
				>
			</div>
		{/if}
	</div>
</RayShell>
