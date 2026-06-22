<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		serveApplications,
		type ServePayload,
		type ServeApplication,
		type ServeDeployment,
	} from '$lib/api';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { SortHeader } from '@your-repo/oxen/sort-header';
	import { Card } from '$lib/components/ui/card';
	import { Badge } from '@your-repo/oxen/badge';
	import {
		ServerCog,
		ScanText,
		Boxes,
		ArrowDownUp,
		Sparkles,
		Activity,
		Gauge,
		Network,
		ChevronRight,
		ExternalLink,
		TriangleAlert,
	} from 'lucide-svelte';

	let payload = $state<ServePayload | null>(null);
	let error = $state<string | null>(null);
	let timer: ReturnType<typeof setInterval> | null = null;

	async function refresh() {
		try {
			payload = await serveApplications();
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

	const apps = $derived.by<ServeApplication[]>(() =>
		Object.values(payload?.applications ?? {}).sort((a, b) => a.name.localeCompare(b.name)),
	);
	const runningApps = $derived(apps.filter((a) => a.status === 'RUNNING').length);
	const allDeployments = $derived(apps.flatMap((a) => Object.values(a.deployments ?? {})));
	const healthyDeployments = $derived(allDeployments.filter((d) => d.status === 'HEALTHY').length);
	const liveReplicas = $derived(allDeployments.reduce((n, d) => n + (d.replicas?.length ?? 0), 0));
	const proxies = $derived(Object.values(payload?.proxies ?? {}));
	const ctrl = $derived(payload?.controller_health_metrics);

	let proxySortKey = $state('node_ip');
	let proxySortDir = $state<'asc' | 'desc'>('asc');
	function setProxySort(col: string) {
		if (proxySortKey === col) proxySortDir = proxySortDir === 'asc' ? 'desc' : 'asc';
		else {
			proxySortKey = col;
			proxySortDir = 'asc';
		}
	}
	const sortedProxies = $derived.by(() => {
		const dir = proxySortDir === 'asc' ? 1 : -1;
		const val = (p: (typeof proxies)[number]) =>
			({ status: p.status, node_ip: p.node_ip, pod: p.node_instance_id, log: p.log_file_path })[
				proxySortKey
			] ?? null;
		return [...proxies].sort((x, y) => {
			const a = val(x);
			const b = val(y);
			if (a == null && b == null) return 0;
			if (a == null) return 1;
			if (b == null) return -1;
			return String(a).localeCompare(String(b), undefined, { numeric: true }) * dir;
		});
	});

	const stats = $derived([
		{
			label: 'Applications',
			value: runningApps,
			total: apps.length as number | null,
			dot: 'bg-emerald-500',
			sub: 'running / total',
		},
		{
			label: 'Deployments',
			value: healthyDeployments,
			total: allDeployments.length as number | null,
			dot: 'bg-sky-500',
			sub: 'healthy / total',
		},
		{
			label: 'Live replicas',
			value: liveReplicas,
			total: null as number | null,
			dot: 'bg-violet-500',
			sub: '',
		},
		{
			label: 'Proxies',
			value: proxies.filter((p) => p.status === 'HEALTHY').length,
			total: proxies.length as number | null,
			dot: 'bg-fuchsia-500',
			sub: 'healthy / total',
		},
		{
			label: 'Controller',
			value: fmtUptime(ctrl?.uptime_s),
			total: null as number | null,
			dot: 'bg-amber-500',
			sub: 'uptime',
			str: true,
		},
	]);

	// ── helpers ───────────────────────────────────────────────────────────
	const ok = (s: string) => s === 'RUNNING' || s === 'HEALTHY';
	const transitional = (s: string) => ['DEPLOYING', 'UPDATING', 'NOT_STARTED'].includes(s);

	function statusVariant(status: string): 'success' | 'secondary' | 'destructive' {
		if (ok(status)) return 'success';
		if (transitional(status)) return 'secondary';
		return 'destructive';
	}
	function accent(status: string): string {
		if (ok(status)) return 'border-l-emerald-500';
		if (transitional(status)) return 'border-l-amber-500';
		return 'border-l-destructive';
	}
	function tile(status: string): string {
		if (ok(status)) return 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400';
		if (transitional(status)) return 'bg-amber-500/12 text-amber-600 dark:text-amber-400';
		return 'bg-destructive/12 text-destructive';
	}
	function appIcon(name: string): typeof ServerCog {
		const n = name.toLowerCase();
		if (n.includes('htr')) return ScanText;
		if (n.includes('embed')) return Boxes;
		if (n.includes('rerank')) return ArrowDownUp;
		if (/gemma|qwen|llm|gpt|llama/.test(n)) return Sparkles;
		return ServerCog;
	}
	function resourceChip(key: string): string {
		if (key === 'GPU') return 'bg-violet-500/15 text-violet-600 dark:text-violet-400';
		if (key === 'CPU') return 'bg-sky-500/15 text-sky-600 dark:text-sky-400';
		return 'bg-muted text-muted-foreground';
	}
	function replicaState(s: string): 'success' | 'secondary' | 'destructive' {
		if (s === 'RUNNING') return 'success';
		if (['STARTING', 'UPDATING', 'PENDING_ALLOCATION', 'RECOVERING'].includes(s))
			return 'secondary';
		return 'destructive';
	}

	function fmtAgo(s: number | undefined): string {
		if (!s) return '';
		return fmtUptime(Date.now() / 1000 - s) + ' ago';
	}
	function fmtUptime(secs: number | undefined): string {
		if (secs == null) return '—';
		secs = Math.max(0, secs);
		if (secs < 60) return `${Math.floor(secs)}s`;
		if (secs < 3600) return `${Math.floor(secs / 60)}m`;
		if (secs < 86400) return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
		return `${Math.floor(secs / 86400)}d ${Math.floor((secs % 86400) / 3600)}h`;
	}
	const pod = (id: string | undefined) => (id ?? '').replace(/^kuberay-ai-dev-cluster-/, '') || '—';

	// Config rows worth surfacing (kept compact). Secrets handled separately.
	function configRows(d: ServeDeployment): { k: string; v: string }[] {
		const c = d.deployment_config;
		if (!c) return [];
		const o = c.ray_actor_options;
		const rows: { k: string; v: string }[] = [
			['num_replicas', c.num_replicas],
			['max_ongoing_requests', c.max_ongoing_requests],
			['max_queued_requests', c.max_queued_requests === -1 ? '∞' : c.max_queued_requests],
			[
				'rolling_update_%',
				c.rolling_update_percentage != null ? c.rolling_update_percentage * 100 + '%' : undefined,
			],
			['health_check_period_s', c.health_check_period_s],
			['health_check_timeout_s', c.health_check_timeout_s],
			['graceful_shutdown_timeout_s', c.graceful_shutdown_timeout_s],
			['actor num_cpus', o?.num_cpus],
			['actor num_gpus', o?.num_gpus],
			['router', c.request_router_config?.request_router_class?.split(':').pop()],
		]
			.filter(([, v]) => v !== undefined && v !== null)
			.map(([k, v]) => ({ k: String(k), v: String(v) }));
		return rows;
	}
</script>

<svelte:head>
	<title>Serve — RASK</title>
</svelte:head>

<RayShell title="Serve">
	<div class="flex flex-col gap-4 p-6 text-sm">
		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 text-destructive p-3">{error}</Card>
		{/if}

		{#if payload && !apps.length && !error}
			<Card class="border-amber-500/40 bg-amber-500/10 p-4 text-center">
				<ServerCog class="text-muted-foreground mx-auto mb-2 h-6 w-6" />
				No Serve applications are deployed on the cluster.
			</Card>
		{/if}

		{#if apps.length}
			<!-- Summary strip -->
			<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
				{#each stats as s (s.label)}
					<Card class="relative overflow-hidden p-4">
						<div class="absolute inset-x-0 top-0 h-0.5 {s.dot}"></div>
						<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
							{s.label}
						</div>
						<div class="mt-1 font-mono text-2xl tabular-nums">
							{s.value}{#if s.total !== null}<span class="text-muted-foreground text-base"
									>/{s.total}</span
								>{/if}
						</div>
						{#if s.sub}<div class="text-muted-foreground text-xs">{s.sub}</div>{/if}
					</Card>
				{/each}
			</section>

			<!-- Controller & HTTP -->
			{#if payload}
				<Card class="p-4">
					<div
						class="text-muted-foreground mb-3 flex items-center gap-1.5 text-[11px] font-medium tracking-wide uppercase"
					>
						<Gauge class="h-3.5 w-3.5" /> Controller & ingress
					</div>
					<div
						class="grid grid-cols-2 gap-x-6 gap-y-1.5 font-mono text-xs sm:grid-cols-3 lg:grid-cols-4"
					>
						{#each [{ k: 'controller node', v: payload.controller_info?.node_ip }, { k: 'controller pod', v: pod(payload.controller_info?.node_instance_id) }, { k: 'control loops', v: ctrl?.num_control_loops?.toLocaleString() }, { k: 'loops / s', v: ctrl?.loops_per_second?.toFixed(1) }, { k: 'asyncio tasks', v: ctrl?.num_asyncio_tasks }, { k: 'proxy location', v: payload.proxy_location }, { k: 'http host', v: payload.http_options?.host }, { k: 'target capacity', v: payload.target_capacity ?? 'unset' }] as row (row.k)}
							<div class="flex justify-between gap-2 border-b border-dashed py-0.5">
								<span class="text-muted-foreground">{row.k}</span>
								<span class="truncate">{row.v ?? '—'}</span>
							</div>
						{/each}
					</div>
				</Card>
			{/if}

			<!-- Proxies -->
			{#if proxies.length}
				<Card class="overflow-hidden">
					<div
						class="text-muted-foreground border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
					>
						Proxies ({proxies.length})
					</div>
					<div class="overflow-auto">
						<table class="w-full border-collapse text-xs">
							<thead class="bg-card text-left">
								<tr class="border-b">
									<SortHeader
										label="status"
										col="status"
										sortKey={proxySortKey}
										sortDir={proxySortDir}
										onsort={setProxySort}
									/>
									<SortHeader
										label="node ip"
										col="node_ip"
										sortKey={proxySortKey}
										sortDir={proxySortDir}
										onsort={setProxySort}
									/>
									<SortHeader
										label="pod"
										col="pod"
										sortKey={proxySortKey}
										sortDir={proxySortDir}
										onsort={setProxySort}
									/>
									<SortHeader
										label="log"
										col="log"
										sortKey={proxySortKey}
										sortDir={proxySortDir}
										onsort={setProxySort}
									/>
								</tr>
							</thead>
							<tbody>
								{#each sortedProxies as p (p.node_id)}
									<tr class="border-border/40 hover:bg-muted/40 border-b last:border-b-0">
										<td class="px-3 py-1.5">
											<Badge variant={statusVariant(p.status ?? '')}>{p.status ?? '—'}</Badge>
										</td>
										<td class="px-3 py-1.5 font-mono">{p.node_ip ?? '—'}</td>
										<td class="px-3 py-1.5 font-mono">{pod(p.node_instance_id)}</td>
										<td class="text-muted-foreground px-3 py-1.5 font-mono text-[10px]">
											{p.log_file_path ?? '—'}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				</Card>
			{/if}

			<!-- Application cards -->
			{#each apps as app (app.name)}
				{@const deployments = Object.values(app.deployments ?? {})}
				{@const Icon = appIcon(app.name)}
				{@const topo = app.deployment_topology}
				<Card class="overflow-hidden border-l-2 {accent(app.status)}">
					<div class="flex flex-wrap items-center gap-3 px-4 py-3">
						<div
							class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg {tile(
								app.status,
							)}"
						>
							<Icon class="h-5 w-5" />
						</div>
						<div class="min-w-0">
							<div class="flex flex-wrap items-center gap-2">
								<span class="truncate font-semibold tracking-tight">{app.name}</span>
								<Badge variant={statusVariant(app.status)}>{app.status}</Badge>
								{#if app.source}<span
										class="text-muted-foreground text-[10px] tracking-wide uppercase"
										>{app.source}</span
									>{/if}
								{#if app.external_scaler_enabled}<Badge variant="outline" class="text-[10px]"
										>ext-scaler</Badge
									>{/if}
							</div>
							<div
								class="text-muted-foreground mt-0.5 flex flex-wrap items-center gap-2 font-mono text-xs"
							>
								{#if app.route_prefix}<span>{app.route_prefix}</span>{/if}
								{#if app.docs_path}
									<span class="text-muted-foreground/70 inline-flex items-center gap-0.5">
										<ExternalLink class="h-3 w-3" />{app.route_prefix}{app.docs_path}
									</span>
								{/if}
							</div>
						</div>
						<div class="text-muted-foreground ml-auto flex shrink-0 items-center gap-1 text-xs">
							<Activity class="h-3.5 w-3.5" />
							{deployments.length} deployment{deployments.length === 1 ? '' : 's'}
							{#if app.last_deployed_time_s}
								<span class="text-muted-foreground/60"
									>· deployed {fmtAgo(app.last_deployed_time_s)}</span
								>
							{/if}
						</div>
					</div>

					{#if app.message}
						<div class="text-muted-foreground border-t px-4 py-1.5 font-mono text-[11px]">
							{app.message}
						</div>
					{/if}

					{#if topo && Object.keys(topo.nodes).length > 1}
						<div
							class="text-muted-foreground flex flex-wrap items-center gap-1.5 border-t px-4 py-2 text-xs"
						>
							<Network class="h-3.5 w-3.5" />
							{#each Object.values(topo.nodes) as node (node.name)}
								<span class="bg-muted rounded px-1.5 py-0.5 font-mono text-[10px]">
									{node.name}{#if node.is_ingress}<span class="text-primary"> ◂ingress</span>{/if}
								</span>
								{#each node.outbound_deployments as edge (edge.name)}
									<ChevronRight class="h-3 w-3" /><span
										class="text-muted-foreground/70 font-mono text-[10px]">{edge.name}</span
									>
								{/each}
							{/each}
						</div>
					{/if}

					<!-- Deployments -->
					<div class="border-t">
						{#each deployments as d (d.name)}
							{@const live = d.replicas?.length ?? 0}
							{@const target = d.target_num_replicas ?? live}
							{@const resources = Object.entries(d.required_resources ?? {}).filter(([, v]) => v)}
							{@const cfg = configRows(d)}
							{@const env = d.deployment_config?.ray_actor_options?.runtime_env}
							{@const dead = d.recent_dead_replicas ?? []}
							<div class="border-b last:border-b-0">
								<div
									class="hover:bg-muted/30 flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-2.5"
								>
									<span class="font-mono text-xs font-medium">{d.name}</span>
									<Badge variant={statusVariant(d.status)} class="scale-90">{d.status}</Badge>
									{#if d.status_trigger}<span class="text-muted-foreground/70 text-[10px]"
											>{d.status_trigger}</span
										>{/if}

									<div class="ml-auto flex items-center gap-2">
										{#if target > 0 && target <= 16}
											<div class="flex gap-0.5">
												{#each Array.from({ length: target }) as _, i (i)}
													<span
														class="h-3.5 w-1.5 rounded-full {i < live
															? 'bg-emerald-500'
															: 'bg-muted-foreground/20'}"
													></span>
												{/each}
											</div>
										{/if}
										<span class="text-muted-foreground font-mono text-xs tabular-nums"
											>{live}/{target}</span
										>
									</div>

									{#if resources.length}
										<div class="flex w-full flex-wrap gap-1 sm:w-auto">
											{#each resources as [k, v] (k)}
												<span
													class="rounded-md px-1.5 py-0.5 text-[10px] font-medium {resourceChip(k)}"
													>{v}× {k}</span
												>
											{/each}
										</div>
									{/if}
								</div>

								{#if d.message}
									<div class="text-muted-foreground px-4 pb-1.5 font-mono text-[11px]">
										{d.message}
									</div>
								{/if}

								{#if dead.length}
									<div class="text-destructive flex items-center gap-1.5 px-4 pb-1.5 text-[11px]">
										<TriangleAlert class="h-3.5 w-3.5" />
										{dead.length} recent dead replica{dead.length === 1 ? '' : 's'}:
										<span class="font-mono">{dead.map((r) => r.replica_id).join(', ')}</span>
									</div>
								{/if}

								<!-- Replicas -->
								{#if live}
									<details class="group">
										<summary
											class="text-muted-foreground hover:bg-muted/40 flex cursor-pointer list-none items-center gap-1 px-4 py-1.5 text-[11px]"
										>
											<ChevronRight class="h-3 w-3 transition-transform group-open:rotate-90" /> Replicas
											({live})
										</summary>
										<div class="overflow-auto px-2 pb-2">
											<table class="w-full border-collapse text-[11px]">
												<thead class="text-muted-foreground text-left">
													<tr>
														{#each ['replica', 'state', 'node ip', 'pod', 'pid', 'uptime', 'log'] as h (h)}
															<th class="px-2 py-1 font-medium">{h}</th>
														{/each}
													</tr>
												</thead>
												<tbody>
													{#each d.replicas as r (r.replica_id)}
														<tr class="border-border/30 border-t">
															<td class="px-2 py-1 font-mono">{r.replica_id}</td>
															<td class="px-2 py-1"
																><Badge variant={replicaState(r.state)} class="scale-90"
																	>{r.state}</Badge
																></td
															>
															<td class="px-2 py-1 font-mono">{r.node_ip ?? '—'}</td>
															<td class="px-2 py-1 font-mono">{pod(r.node_instance_id)}</td>
															<td class="px-2 py-1 font-mono tabular-nums">{r.pid ?? '—'}</td>
															<td class="px-2 py-1 font-mono tabular-nums"
																>{fmtUptime(
																	r.start_time_s ? Date.now() / 1000 - r.start_time_s : undefined,
																)}</td
															>
															<td class="text-muted-foreground px-2 py-1 font-mono text-[10px]"
																>{r.log_file_path ?? '—'}</td
															>
														</tr>
													{/each}
												</tbody>
											</table>
										</div>
									</details>
								{/if}

								<!-- Config -->
								{#if cfg.length || env}
									<details class="group">
										<summary
											class="text-muted-foreground hover:bg-muted/40 flex cursor-pointer list-none items-center gap-1 px-4 py-1.5 text-[11px]"
										>
											<ChevronRight class="h-3 w-3 transition-transform group-open:rotate-90" /> Config
										</summary>
										<div class="space-y-2 px-4 pb-3 pt-1">
											<div
												class="grid grid-cols-2 gap-x-6 gap-y-1 font-mono text-[11px] sm:grid-cols-3"
											>
												{#each cfg as row (row.k)}
													<div class="flex justify-between gap-2 border-b border-dashed py-0.5">
														<span class="text-muted-foreground">{row.k}</span><span>{row.v}</span>
													</div>
												{/each}
											</div>
											{#if env?.uv?.length || env?.pip?.length}
												<div class="flex flex-wrap items-center gap-1">
													<span class="text-muted-foreground text-[10px] uppercase">deps</span>
													{#each [...(env.uv ?? []), ...(env.pip ?? [])] as p (p)}
														<span class="bg-muted rounded px-1.5 py-0.5 font-mono text-[10px]"
															>{p}</span
														>
													{/each}
												</div>
											{/if}
											{#if env?.working_dir}
												<div class="font-mono text-[10px]">
													<span class="text-muted-foreground uppercase">working_dir</span>
													{env.working_dir}
												</div>
											{/if}
											{#if env?.env_vars && Object.keys(env.env_vars).length}
												<div class="flex flex-wrap items-center gap-1 font-mono text-[10px]">
													<span class="text-muted-foreground uppercase">env</span>
													{#each Object.keys(env.env_vars) as k (k)}
														<span class="bg-muted rounded px-1.5 py-0.5"
															>{k}=<span class="text-muted-foreground">••••••</span></span
														>
													{/each}
												</div>
											{/if}
										</div>
									</details>
								{/if}
							</div>
						{/each}
					</div>
				</Card>
			{/each}
		{/if}
	</div>
</RayShell>
