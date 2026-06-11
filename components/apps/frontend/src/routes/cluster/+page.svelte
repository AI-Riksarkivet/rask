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

	function mbGb(mb: number): number {
		return mb / 1024;
	}

	function pct(used: number, total: number): number {
		if (!total) return 0;
		return Math.min(100, (used / total) * 100);
	}

	// Real host memory aggregated across nodes (Ray's logical memory is ~0 used).
	let realMem = $derived.by(() => {
		const ns = payload?.nodes ?? [];
		return {
			used: ns.reduce((a, n) => a + (n.host_mem_used ?? 0), 0),
			total: ns.reduce((a, n) => a + (n.host_mem_total ?? 0), 0)
		};
	});
</script>

<svelte:head>
	<title>Cluster — RASK</title>
</svelte:head>

<RayShell title="Cluster">
	<div class="flex flex-col gap-4 p-6 text-sm">
		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 text-destructive p-3">{error}</Card>
		{/if}

		{#if payload && !payload.ok}
			<Card class="border-amber-500/40 bg-amber-500/10 p-3">
				Ray dashboard unreachable at <span class="font-mono">{payload.dashboard_url}</span>
				{#if payload.error}
					<div class="text-muted-foreground mt-1 text-xs">{payload.error}</div>
				{/if}
			</Card>
		{/if}

		{#if payload?.ok && payload.total_resources}
			{@const tr = payload.total_resources}
			{@const ur = payload.used_resources!}

			<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
				<Card class="p-4">
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						Nodes
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">
						{payload.alive_count}/{payload.node_count}
					</div>
					<div class="text-muted-foreground text-xs">alive / total</div>
				</Card>
				<Card class="p-4">
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						GPU
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">
						{ur.GPU.toFixed(1)}<span class="text-muted-foreground text-base"
							>/{tr.GPU.toFixed(0)}</span
						>
					</div>
					<div class="bg-muted mt-1.5 h-1.5 w-full overflow-hidden rounded-full">
						<div
							class="h-full bg-emerald-500 transition-all"
							style:width={`${pct(ur.GPU, tr.GPU)}%`}
						></div>
					</div>
				</Card>
				<Card class="p-4">
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						CPU
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">
						{ur.CPU.toFixed(0)}<span class="text-muted-foreground text-base"
							>/{tr.CPU.toFixed(0)}</span
						>
					</div>
					<div class="bg-muted mt-1.5 h-1.5 w-full overflow-hidden rounded-full">
						<div
							class="h-full bg-sky-500 transition-all"
							style:width={`${pct(ur.CPU, tr.CPU)}%`}
						></div>
					</div>
				</Card>
				<Card class="p-4">
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						Memory <span class="text-muted-foreground/60 normal-case">(host)</span>
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">
						{bytesGb(realMem.used).toFixed(0)}<span class="text-muted-foreground text-base"
							>/{bytesGb(realMem.total).toFixed(0)} GiB</span
						>
					</div>
					<div class="bg-muted mt-1.5 h-1.5 w-full overflow-hidden rounded-full">
						<div
							class="h-full bg-violet-500 transition-all"
							style:width={`${pct(realMem.used, realMem.total)}%`}
						></div>
					</div>
				</Card>
			</section>

			<Card class="overflow-hidden">
				<div
					class="text-muted-foreground border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
				>
					Nodes
				</div>
				<div class="max-h-[60vh] overflow-auto">
					<table class="w-full border-collapse text-xs">
						<thead class="bg-card sticky top-0 z-10 text-left">
							<tr class="border-b">
								{#each ['state', 'tier', 'node', 'GPU (util · resv)', 'VRAM · temp', 'CPU', 'memory (host)'] as h (h)}
									<th class="text-muted-foreground px-3 py-2 font-medium">{h}</th>
								{/each}
							</tr>
						</thead>
						<tbody>
							{#each payload.nodes ?? [] as n (n.node_id)}
								{@const gpuT = n.resources_total.GPU ?? 0}
								{@const gpuU = n.resources_used.GPU ?? 0}
								{@const cpuT = n.resources_total.CPU ?? 0}
								{@const cpuU = n.resources_used.CPU ?? 0}
								<tr class="border-border/40 hover:bg-muted/40 border-b">
									<td class="px-3 py-1.5">
										{#if n.alive}
											<Badge variant="success">alive</Badge>
										{:else}
											<Badge variant="destructive">dead</Badge>
										{/if}
										{#if n.is_head}
											<Badge variant="secondary" class="ml-1">head</Badge>
										{/if}
									</td>
									<td class="px-3 py-1.5 font-mono">{n.node_type ?? '—'}</td>
									<td class="px-3 py-1.5">
										<div class="font-mono">{n.hostname ?? n.node_id?.slice(0, 12) ?? '—'}</div>
										<div class="text-muted-foreground font-mono text-[10px]">{n.node_ip ?? ''}</div>
									</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{#if n.gpus.length}
											{#each n.gpus as g (g.index)}
												<div title={g.name ?? ''}>
													{(g.utilization_percent ?? 0).toFixed(0)}%
													<span class="text-muted-foreground"
														>· {gpuU.toFixed(1)}/{gpuT.toFixed(0)}</span
													>
												</div>
											{/each}
										{:else}
											<span class="text-muted-foreground">—</span>
										{/if}
									</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{#if n.gpus.length}
											{#each n.gpus as g (g.index)}
												<div>
													{mbGb(g.memory_used_mb ?? 0).toFixed(1)}/{mbGb(
														g.memory_total_mb ?? 0
													).toFixed(0)} GB
													<span class="text-muted-foreground">· {(g.temperature_c ?? 0).toFixed(0)}°C</span
													>
												</div>
											{/each}
										{:else}
											<span class="text-muted-foreground">—</span>
										{/if}
									</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{(n.host_cpu_percent ?? 0).toFixed(1)}%
										<span class="text-muted-foreground">· {cpuU.toFixed(0)}/{cpuT.toFixed(0)}</span>
									</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{bytesGb(n.host_mem_used ?? 0).toFixed(1)}/{bytesGb(n.host_mem_total ?? 0).toFixed(
											0
										)} GiB
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</Card>

			<div class="text-muted-foreground text-xs">
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
