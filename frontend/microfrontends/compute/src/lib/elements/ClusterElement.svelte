<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-compute-cluster>` — Ray nodes + capacity, VISUALLY IDENTICAL to the zone's
	 * /compute/cluster page: same summary Cards, same Badge variants, same node tiles and per-GPU
	 * bars, same utility classes (compiled into this bundle by elements.css — the host cannot
	 * generate them). The mount stamp + poll counter stay as the no-remount witness; rows dispatch
	 * the rask:select contract event instead of navigating.
	 */
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { rayCluster, type RayClusterPayload, type RayNode } from '@rask/api';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { Server, Cpu } from '@lucide/svelte';
	import { RayPoll } from './ray-poll.svelte';

	let { pollms = 5000 }: { pollms?: number } = $props();
	const poll = new RayPoll<RayClusterPayload>((f) => rayCluster(f));
	$effect(() => poll.start(pollms));

	const payload = $derived(poll.data);
	const nodes = $derived(poll.data?.nodes ?? []);

	// Mirrored from routes/cluster/+page.svelte — the same numbers must be formatted the same way.
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
	const short = (s: string | null | undefined) =>
		(s ?? '').replace(/^kuberay-ai-dev-cluster-/, '') || '—';

	function nodeIcon(n: RayNode): typeof Server {
		return n.is_head ? Server : Cpu;
	}
	// Tile colored by role; dead nodes go red.
	function nodeTile(n: RayNode): string {
		if (!n.alive) return 'bg-destructive/12 text-destructive';
		if (n.is_head) return 'bg-amber-500/12 text-amber-600 dark:text-amber-400';
		if ((n.resources_total.GPU ?? 0) > 0)
			return 'bg-violet-500/12 text-violet-600 dark:text-violet-400';
		return 'bg-sky-500/12 text-sky-600 dark:text-sky-400';
	}

	// Real host memory aggregated across nodes (Ray's logical memory is ~0 used).
	const realMem = $derived.by(() => ({
		used: nodes.reduce((a, n) => a + (n.host_mem_used ?? 0), 0),
		total: nodes.reduce((a, n) => a + (n.host_mem_total ?? 0), 0),
	}));

	function select(node: HTMLElement, n: RayNode) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-compute-cluster',
					kind: 'ray-node',
					id: n.node_id ?? n.hostname ?? 'node',
					label: n.hostname ?? n.node_ip ?? 'node',
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if poll.error !== null}
		<p class="text-destructive text-sm">Ray unreachable: {poll.error}</p>
	{:else if payload && !payload.ok}
		<Card class="border-amber-500/40 bg-amber-500/10 p-3 text-sm">
			Ray dashboard unreachable at <span class="font-mono">{payload.dashboard_url}</span>
			{#if payload.error}
				<div class="text-muted-foreground mt-1 text-xs">{payload.error}</div>
			{/if}
		</Card>
	{:else if nodes.length === 0}
		<p class="text-muted-foreground text-sm">No nodes reported.</p>
	{:else}
		<div class="flex flex-col gap-4 text-sm">
			{#if payload?.total_resources && payload.used_resources}
				{@const tr = payload.total_resources}
				{@const ur = payload.used_resources}

				<!-- Summary strip -->
				<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
					<Card class="relative overflow-hidden p-4">
						<div class="absolute inset-x-0 top-0 h-0.5 bg-emerald-500"></div>
						<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">Nodes</div>
						<div class="mt-1 font-mono text-2xl tabular-nums">
							{payload.alive_count}<span class="text-muted-foreground text-base"
								>/{payload.node_count}</span
							>
						</div>
						<div class="text-muted-foreground text-xs">alive / total</div>
					</Card>

					<Card class="relative overflow-hidden p-4">
						<div class="absolute inset-x-0 top-0 h-0.5 bg-violet-500"></div>
						<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">GPU</div>
						<div class="mt-1 font-mono text-2xl tabular-nums">
							{ur.GPU.toFixed(1)}<span class="text-muted-foreground text-base">/{tr.GPU.toFixed(0)}</span>
						</div>
						<div class="bg-muted mt-1.5 h-1.5 w-full overflow-hidden rounded-full">
							<div
								class="h-full bg-violet-500 transition-all"
								style:width={`${pct(ur.GPU, tr.GPU)}%`}
							></div>
						</div>
					</Card>

					<Card class="relative overflow-hidden p-4">
						<div class="absolute inset-x-0 top-0 h-0.5 bg-sky-500"></div>
						<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">CPU</div>
						<div class="mt-1 font-mono text-2xl tabular-nums">
							{ur.CPU.toFixed(0)}<span class="text-muted-foreground text-base">/{tr.CPU.toFixed(0)}</span>
						</div>
						<div class="bg-muted mt-1.5 h-1.5 w-full overflow-hidden rounded-full">
							<div
								class="h-full bg-sky-500 transition-all"
								style:width={`${pct(ur.CPU, tr.CPU)}%`}
							></div>
						</div>
					</Card>

					<Card class="relative overflow-hidden p-4">
						<div class="absolute inset-x-0 top-0 h-0.5 bg-amber-500"></div>
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
								class="h-full bg-amber-500 transition-all"
								style:width={`${pct(realMem.used, realMem.total)}%`}
							></div>
						</div>
					</Card>
				</section>
			{/if}

			<!-- Nodes -->
			<Card class="overflow-hidden">
				<div
					class="text-muted-foreground border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
				>
					Nodes ({nodes.length})
				</div>
				<div class="max-h-full overflow-auto">
					<table class="w-full border-collapse text-xs">
						<thead class="bg-card sticky top-0 z-10 text-left">
							<tr class="border-b">
								<th class="px-3 py-2">state</th>
								<th class="px-3 py-2">node</th>
								<th class="px-3 py-2">tier</th>
								<th class="px-3 py-2">GPU util</th>
								<th class="px-3 py-2">VRAM · temp</th>
								<th class="px-3 py-2">CPU</th>
								<th class="px-3 py-2">memory (host)</th>
							</tr>
						</thead>
						<tbody>
							{#each nodes as n (n.node_id)}
								{@const gpuT = n.resources_total.GPU ?? 0}
								{@const gpuU = n.resources_used.GPU ?? 0}
								{@const cpuT = n.resources_total.CPU ?? 0}
								{@const cpuU = n.resources_used.CPU ?? 0}
								{@const NodeIcon = nodeIcon(n)}
								<tr
									class="border-border/40 hover:bg-muted/40 cursor-pointer border-b {n.alive
										? ''
										: 'opacity-60'}"
									onclick={(e) => select(e.currentTarget, n)}
								>
									<td class="px-3 py-1.5">
										{#if n.alive}
											<Badge variant="success">alive</Badge>
										{:else}
											<Badge variant="destructive">dead</Badge>
										{/if}
										{#if n.is_head}<Badge variant="secondary" class="ml-1">head</Badge>{/if}
									</td>
									<td class="px-3 py-1.5">
										<div class="flex items-center gap-2">
											<div
												class="flex h-6 w-6 shrink-0 items-center justify-center rounded-md {nodeTile(
													n,
												)}"
											>
												<NodeIcon class="h-3.5 w-3.5" />
											</div>
											<div class="min-w-0">
												<div class="truncate font-mono" title={n.hostname ?? ''}>
													{short(n.hostname) === '—' ? (n.node_id?.slice(0, 12) ?? '—') : short(n.hostname)}
												</div>
												<div class="text-muted-foreground font-mono text-[10px]">
													{n.node_ip ?? ''}
												</div>
											</div>
										</div>
									</td>
									<td class="px-3 py-1.5 font-mono">{n.node_type ?? '—'}</td>

									<!-- per-GPU utilisation bars; node-level reservation shown once -->
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{#if n.gpus.length}
											<div class="flex flex-col gap-0.5">
												{#each n.gpus as g, i (g.uuid ?? g.index ?? i)}
													<div
														class="flex items-center gap-1.5"
														title={`${g.name ?? ''}${g.uuid ? `\n${g.uuid}` : ''}`}
													>
														<div class="bg-muted h-1.5 w-12 shrink-0 overflow-hidden rounded-full">
															<div
																class="h-full bg-emerald-500"
																style:width={`${g.utilization_percent ?? 0}%`}
															></div>
														</div>
														<span class="w-8 text-right">{(g.utilization_percent ?? 0).toFixed(0)}%</span>
													</div>
												{/each}
											</div>
											{#if gpuT}
												<div class="text-muted-foreground text-[10px]">
													{gpuU.toFixed(2)}/{gpuT.toFixed(0)} reserved
												</div>
											{/if}
										{:else}
											<span class="text-muted-foreground">—</span>
										{/if}
									</td>

									<td class="px-3 py-1.5 font-mono tabular-nums">
										{#if n.gpus.length}
											{#each n.gpus as g, i (g.uuid ?? g.index ?? i)}
												<div>
													{mbGb(g.memory_used_mb ?? 0).toFixed(1)}/{mbGb(g.memory_total_mb ?? 0).toFixed(0)} GB
													<span class="text-muted-foreground">· {(g.temperature_c ?? 0).toFixed(0)}°C</span>
												</div>
											{/each}
										{:else}
											<span class="text-muted-foreground">—</span>
										{/if}
									</td>

									<!-- host CPU% bar + reserved -->
									<td class="px-3 py-1.5 font-mono tabular-nums">
										<div class="flex items-center gap-1.5">
											<div class="bg-muted h-1.5 w-12 shrink-0 overflow-hidden rounded-full">
												<div
													class="h-full bg-sky-500"
													style:width={`${n.host_cpu_percent ?? 0}%`}
												></div>
											</div>
											<span>{(n.host_cpu_percent ?? 0).toFixed(0)}%</span>
										</div>
										<div class="text-muted-foreground text-[10px]">
											{cpuU.toFixed(0)}/{cpuT.toFixed(0)} reserved
										</div>
									</td>

									<td class="px-3 py-1.5 font-mono tabular-nums">
										{bytesGb(n.host_mem_used ?? 0).toFixed(1)}/{bytesGb(n.host_mem_total ?? 0).toFixed(0)} GiB
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</Card>
		</div>
	{/if}
</div>
