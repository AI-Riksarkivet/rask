<!--
	Actors — the ZONE'S view, rendered by BOTH `/compute/actors` and the dock's ActorsPanel.

	It used to exist twice: the route, and a hand-written ~50-line table in the panel over the same
	remote. That is MIRRORING — the exact failure that retired the cross-zone compositor, reintroduced
	inside the zone where nothing was stopping it. The two drifted immediately: the page sorts,
	filters and links; the panel showed four columns and no controls.

	One component now. The panel is a sizing wrapper around it, so a column added here appears in both.
-->
<script lang="ts">
	import { liveRead } from '@rask/api/live';
	import { rayClock } from '$lib/live/ray-clock.svelte';
	import { base } from '$app/paths';
	import { type ActorInfo } from '@rask/api';
	import { getActors, getRayCluster } from '$lib/remote/compute.remote';
	import { SortHeader } from '@rask/ui/sort-header';
	import { Card } from '@rask/ui/card';
	import { Badge } from '@rask/ui/badge';
	import {
		Boxes,
		ServerCog,
		Network,
		Gauge,
		ListTree,
		Cpu,
		TriangleAlert,
		ChevronRight,
		FileText,
	} from '@lucide/svelte';

	// THE ONE PATTERN (see lib/remote/compute.remote.ts): cached remote queries,
	// polled below via `.refresh().catch()`, read imperatively (`.current`) for
	// flicker-free refresh. Cluster (node names) is a nicety — its failure is
	// tolerated independently of the actors list.
	const actorsQuery = getActors();
	const clusterQuery = getRayCluster();
	const actors = $derived(actorsQuery.current ?? []);
	const nodes = $derived(clusterQuery.current?.nodes ?? []);
	const error = $derived(actorsQuery.error ? String(actorsQuery.error) : null);

	let stateFilter = $state<'all' | 'alive' | 'dead'>('all');
	let q = $state('');
	let expanded = $state<Set<string>>(new Set());
	let sortKey = $state('state');
	let sortDir = $state<'asc' | 'desc'>('asc');

	function setSort(col: string) {
		if (sortKey === col) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		else {
			sortKey = col;
			sortDir = 'asc';
		}
	}

	// THE ZONE'S ONE CLOCK. This used to be a private `setInterval` here, and in three sibling boards,
	// each re-reading the same shared no-arg cached queries on its own phase. `$lib/live/ray-clock`
	// holds the single interval and the POLL REASON for all of them; `liveRead` gives the
	// unconditional first read. `.catch(() => {})` stays mandatory per refresh — one uncaught
	// rejection evicts that query from cache and silently kills its updates, and it is the catch, not
	// a separate timer, that stops one failing query taking the others with it.
	$effect(() => rayClock.subscribe());
	liveRead(
		() => rayClock.cursor,
		() => {
			actorsQuery.refresh().catch(() => {});
			clusterQuery.refresh().catch(() => {});
		},
	);

	function toggle(id: string) {
		const s = new Set(expanded);
		if (s.has(id)) s.delete(id);
		else s.add(id);
		expanded = s;
	}

	const nodeMap = $derived(new Map(nodes.map((n) => [n.node_id, n])));
	const aliveCount = $derived(actors.filter((a) => a.state === 'ALIVE').length);
	const deadCount = $derived(actors.filter((a) => a.state === 'DEAD').length);
	const totalRestarts = $derived(actors.reduce((n, a) => n + (a.num_restarts ?? 0), 0));
	const classes = $derived(new Set(actors.map((a) => a.class_name)));
	const namespaces = $derived(new Set(actors.map((a) => a.ray_namespace).filter(Boolean)));

	const stats = $derived([
		{
			label: 'Actors',
			value: aliveCount,
			total: actors.length as number | null,
			dot: 'bg-emerald-500',
			sub: 'alive / total',
		},
		{
			label: 'Dead',
			value: deadCount,
			total: null as number | null,
			dot: 'bg-destructive',
			sub: '',
		},
		{
			label: 'Restarts',
			value: totalRestarts,
			total: null as number | null,
			dot: 'bg-amber-500',
			sub: '',
		},
		{
			label: 'Classes',
			value: classes.size,
			total: null as number | null,
			dot: 'bg-sky-500',
			sub: '',
		},
		{
			label: 'Namespaces',
			value: namespaces.size,
			total: null as number | null,
			dot: 'bg-violet-500',
			sub: '',
		},
	]);

	// Filtered set, base-ordered by class so the sort below has a stable tiebreak.
	const filtered = $derived(
		actors
			.filter((a) => stateFilter === 'all' || a.state.toLowerCase() === stateFilter)
			.filter((a) => {
				if (!q) return true;
				const hay =
					`${a.class_name} ${a.name ?? ''} ${a.repr_name ?? ''} ${a.ray_namespace ?? ''}`.toLowerCase();
				return hay.includes(q.toLowerCase());
			})
			.sort((a, b) => a.class_name.localeCompare(b.class_name)),
	);

	function actorVal(a: ActorInfo, key: string): string | number | null {
		switch (key) {
			case 'state':
				return a.state;
			case 'namespace':
				return a.ray_namespace;
			case 'node':
				return nodeLabel(a);
			case 'uptime':
				return actorAge(a);
			case 'cpu':
				return a.cpu_percent;
			case 'tasks':
				return (a.num_running_tasks ?? 0) + (a.num_pending_tasks ?? 0);
			case 'gpu':
				return a.gpu_util ?? a.gpu_mem_mb;
			case 'actor':
				return a.class_name;
			default:
				return null;
		}
	}

	const sorted = $derived.by(() => {
		const dir = sortDir === 'asc' ? 1 : -1;
		return [...filtered].sort((x, y) => {
			const a = actorVal(x, sortKey);
			const b = actorVal(y, sortKey);
			if (a == null && b == null) return 0;
			if (a == null) return 1; // nulls always last
			if (b == null) return -1;
			const c =
				typeof a === 'number' && typeof b === 'number'
					? a - b
					: String(a).localeCompare(String(b), undefined, { numeric: true });
			return c * dir;
		});
	});

	function stateVariant(s: string): 'success' | 'secondary' | 'destructive' {
		if (s === 'ALIVE') return 'success';
		if (s === 'DEAD') return 'destructive';
		return 'secondary';
	}
	function tile(s: string): string {
		if (s === 'ALIVE') return 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400';
		if (s === 'DEAD') return 'bg-destructive/12 text-destructive';
		return 'bg-amber-500/12 text-amber-600 dark:text-amber-400';
	}
	function actorIcon(cls: string): typeof Boxes {
		if (cls.startsWith('ServeReplica')) return ServerCog;
		if (cls.includes('Proxy')) return Network;
		if (cls.includes('Controller')) return Gauge;
		if (cls.includes('Job')) return ListTree;
		if (cls.includes('Worker')) return Cpu;
		return Boxes;
	}
	function resourceChip(key: string): string {
		if (key === 'GPU') return 'bg-violet-500/15 text-violet-600 dark:text-violet-400';
		if (key === 'CPU') return 'bg-sky-500/15 text-sky-600 dark:text-sky-400';
		return 'bg-muted text-muted-foreground';
	}
	const short = (s: string | null | undefined) =>
		(s ?? '').replace(/^kuberay-ai-dev-cluster-/, '') || '';
	function nodeLabel(a: ActorInfo): string {
		const n = a.node_id ? nodeMap.get(a.node_id) : undefined;
		return short(n?.hostname) || a.ip_address || n?.node_ip || a.node_id?.slice(0, 12) || '—';
	}
	function fmtUptime(secs: number | null): string {
		if (secs == null) return '—';
		secs = Math.max(0, secs);
		if (secs < 60) return `${Math.floor(secs)}s`;
		if (secs < 3600) return `${Math.floor(secs / 60)}m`;
		if (secs < 86400) return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
		return `${Math.floor(secs / 86400)}d ${Math.floor((secs % 86400) / 3600)}h`;
	}
	function actorAge(a: ActorInfo): number | null {
		if (!a.start_time_ms) return null;
		const end = a.state === 'DEAD' && a.end_time_ms ? a.end_time_ms : Date.now();
		return (end - a.start_time_ms) / 1000;
	}
	function fmtBytes(b: number | null): string {
		if (b == null) return '—';
		if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`;
		if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`;
		return `${(b / 1024 ** 3).toFixed(1)} GB`;
	}
</script>

<div class="bg-background h-full min-h-0 overflow-auto">
	<div class="flex flex-col gap-4 p-6 text-sm">
		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 text-destructive p-3">{error}</Card>
		{/if}

		{#if !actors.length && !error}
			<Card class="border-amber-500/40 bg-amber-500/10 p-4 text-center">
				<Boxes class="text-muted-foreground mx-auto mb-2 h-6 w-6" />
				No actors on the cluster.
			</Card>
		{/if}

		{#if actors.length}
			<!-- Summary strip -->
			<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
				{#each stats as s (s.label)}
					<Card class="relative overflow-hidden p-4">
						<div class="absolute inset-x-0 top-0 h-0.5 {s.dot}"></div>
						<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
							{s.label}
						</div>
						<div class="mt-1 font-mono text-2xl tabular-nums">
							{s.value}{#if s.total !== null}<span class="text-muted-foreground text-base">/{s.total}</span>{/if}
						</div>
						{#if s.sub}<div class="text-muted-foreground text-xs">{s.sub}</div>{/if}
					</Card>
				{/each}
			</section>

			<!-- Controls -->
			<div class="flex flex-wrap items-center gap-2">
				<div class="flex overflow-hidden rounded-md border">
					{#each ['all', 'alive', 'dead'] as f (f)}
						<button
							class="px-3 py-1 text-xs capitalize transition-colors {stateFilter === f
								? 'bg-primary text-primary-foreground'
								: 'hover:bg-muted'}"
							onclick={() => (stateFilter = f as typeof stateFilter)}
						>
							{f}
						</button>
					{/each}
				</div>
				<input
					class="border-input bg-background focus-visible:ring-ring h-7 w-56 rounded-md border px-2 text-xs focus-visible:ring-1 focus-visible:outline-none"
					placeholder="filter by class / name / namespace…"
					bind:value={q}
				/>
				<span class="text-muted-foreground text-xs">{filtered.length} shown</span>
			</div>

			<!-- Actors table -->
			<Card class="overflow-hidden">
				<div class="max-h-[64vh] overflow-auto">
					<table class="w-full border-collapse text-xs">
						<thead class="bg-card sticky top-0 z-10 text-left">
							<tr class="border-b">
								<SortHeader label="state" col="state" {sortKey} {sortDir} onsort={setSort} />
								<SortHeader
									label="namespace"
									col="namespace"
									{sortKey}
									{sortDir}
									onsort={setSort}
								/>
								<SortHeader label="node" col="node" {sortKey} {sortDir} onsort={setSort} />
								<SortHeader label="uptime" col="uptime" {sortKey} {sortDir} onsort={setSort} />
								<SortHeader label="cpu" col="cpu" {sortKey} {sortDir} onsort={setSort} />
								<SortHeader
									label="tasks (run·pend)"
									col="tasks"
									{sortKey}
									{sortDir}
									onsort={setSort}
								/>
								<SortHeader label="gpu" col="gpu" {sortKey} {sortDir} onsort={setSort} />
								<SortHeader label="actor" col="actor" {sortKey} {sortDir} onsort={setSort} />
								<th class="px-3 py-2"></th>
							</tr>
						</thead>
						<tbody>
							{#each sorted as a (a.actor_id)}
								{@const Icon = actorIcon(a.class_name)}
								{@const open = expanded.has(a.actor_id ?? '')}
								{@const run = a.num_running_tasks ?? 0}
								{@const pend = a.num_pending_tasks ?? 0}
								<tr
									class="border-border/40 hover:bg-muted/40 cursor-pointer border-b {a.state ===
									'DEAD'
										? 'opacity-70'
										: ''}"
									onclick={() => toggle(a.actor_id ?? '')}
								>
									<td class="px-3 py-1.5"
										><Badge variant={stateVariant(a.state)}>{a.state}</Badge></td
									>
									<td class="px-3 py-1.5 font-mono">{a.ray_namespace ?? '—'}</td>
									<td class="px-3 py-1.5 font-mono">{nodeLabel(a)}</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">{fmtUptime(actorAge(a))}</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{#if a.cpu_percent != null}
											<div class="flex items-center gap-1.5">
												<div class="bg-muted h-1.5 w-10 shrink-0 overflow-hidden rounded-full">
													<div
														class="h-full bg-sky-500"
														style:width={`${Math.min(100, a.cpu_percent)}%`}
													></div>
												</div>
												<span>{a.cpu_percent.toFixed(0)}%</span>
											</div>
										{:else}<span class="text-muted-foreground">—</span>{/if}
									</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{#if run || pend}
											<span class={run ? 'text-emerald-600 dark:text-emerald-400' : ''}>{run}</span>
											<span class="text-muted-foreground">·</span>
											<span class={pend ? 'text-amber-600 dark:text-amber-400' : ''}>{pend}</span>
										{:else}<span class="text-muted-foreground">—</span>{/if}
									</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{#if a.gpu_mem_mb != null || a.gpu_util != null}
											{a.gpu_util != null ? `${a.gpu_util.toFixed(0)}%` : ''}
											{#if a.gpu_mem_mb}<span class="text-violet-600 dark:text-violet-400"
													>· {a.gpu_mem_mb} MB</span
												>{/if}
										{:else}<span class="text-muted-foreground">—</span>{/if}
									</td>
									<td class="px-3 py-1.5">
										<div class="flex items-center gap-2">
											<div
												class="flex h-6 w-6 shrink-0 items-center justify-center rounded-md {tile(
													a.state,
												)}"
											>
												<Icon class="h-3.5 w-3.5" />
											</div>
											<div>
												<div class="font-mono" title={a.actor_id ?? ''}>{a.class_name}</div>
												{#if a.death_reason}
													<div
														class="text-destructive flex items-start gap-0.5 font-mono text-[10px]"
														title={a.death_reason}
													>
														<TriangleAlert class="mt-0.5 h-3 w-3 shrink-0" />{a.death_reason}
													</div>
												{:else if a.repr_name}
													<div class="text-muted-foreground font-mono text-[10px]">
														{a.repr_name}
													</div>
												{/if}
											</div>
										</div>
									</td>
									<td class="px-3 py-1.5">
										<ChevronRight
											class="text-muted-foreground h-3.5 w-3.5 transition-transform {open
												? 'rotate-90'
												: ''}"
										/>
									</td>
								</tr>
								{#if open}
									<tr class="bg-muted/20 border-border/40 border-b">
										<td colspan="9" class="px-4 py-3">
											{#if a.node_id}
												<a
													href={`${base}/logviewer?node=${encodeURIComponent(a.node_id)}${a.worker_id ? `&q=${encodeURIComponent(a.worker_id)}` : ''}`}
													class="text-primary mb-2 inline-flex items-center gap-1 text-[11px] hover:underline"
												>
													<FileText class="h-3 w-3" /> view logs
												</a>
											{/if}
											<div
												class="grid grid-cols-2 gap-x-6 gap-y-1 font-mono text-[11px] sm:grid-cols-3 lg:grid-cols-4"
											>
												{#each [{ k: 'actor_id', v: a.actor_id }, { k: 'pid', v: a.pid }, { k: 'worker_id', v: a.worker_id }, { k: 'ip', v: a.ip_address }, { k: 'job_id', v: a.job_id }, { k: 'restarts', v: a.num_restarts }, { k: 'detached', v: String(a.is_detached) }, { k: 'placement_group', v: a.placement_group_id ?? '—' }, { k: 'rss mem', v: fmtBytes(a.rss_bytes) }, { k: 'open fds', v: a.num_fds ?? '—' }, { k: 'tasks executed', v: a.num_executed_tasks ?? '—' }, { k: 'task queue', v: a.task_queue_length ?? '—' }] as row (row.k)}
													<div class="flex justify-between gap-2 border-b border-dashed py-0.5">
														<span class="text-muted-foreground">{row.k}</span>
														<span class="truncate" title={String(row.v ?? '')}>{row.v ?? '—'}</span>
													</div>
												{/each}
											</div>
											{#if Object.keys(a.required_resources ?? {}).length}
												<div class="mt-2 flex flex-wrap items-center gap-1">
													<span class="text-muted-foreground text-[10px] uppercase">reserves</span>
													{#each Object.entries(a.required_resources).filter(([, v]) => v) as [k, v] (k)}
														<span
															class="rounded-md px-1.5 py-0.5 text-[10px] font-medium {resourceChip(
																k,
															)}">{v}× {k}</span
														>
													{/each}
												</div>
											{/if}
										</td>
									</tr>
								{/if}
							{/each}
						</tbody>
					</table>
				</div>
			</Card>
		{/if}
	</div>
</div>
