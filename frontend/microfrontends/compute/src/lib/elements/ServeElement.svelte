<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-compute-serve>` — Ray Serve applications, VISUALLY IDENTICAL to the zone's
	 * /compute/serve list: the same proxies table and the same per-application Cards (status accent,
	 * icon tile, topology strip, deployment rows with the replica pips, and the replicas / config
	 * disclosures), the same Badge variants and the same utility classes — compiled into this bundle
	 * by elements.css, since the host page's Tailwind cannot generate them. The mount stamp + poll
	 * counter stay as the no-remount witness; an application Card dispatches the rask:select contract
	 * event instead of navigating.
	 *
	 * Two page affordances are deliberately NOT mirrored: the summary stat strip and the
	 * "Controller & ingress" grid (page chrome), and the proxies table's SortHeader sorting — a panel
	 * carries no sort state, so its rows hold the page's DEFAULT order (node ip, ascending).
	 */
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { serveApplications, type ServeDeployment, type ServePayload } from '@rask/api';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { RayPoll } from './ray-poll.svelte';
	import {
		ServerCog,
		ScanText,
		Boxes,
		ArrowDownUp,
		Sparkles,
		Activity,
		Network,
		ChevronRight,
		ExternalLink,
		TriangleAlert,
	} from '@lucide/svelte';

	let { pollms = 5000 }: { pollms?: number } = $props();
	const poll = new RayPoll<ServePayload>((f) => serveApplications(f));
	$effect(() => poll.start(pollms));

	const apps = $derived(
		Object.values(poll.data?.applications ?? {}).sort((a, b) => a.name.localeCompare(b.name)),
	);
	// The page's DEFAULT proxy order (node_ip asc) — a panel has no sort header to change it.
	const proxies = $derived(
		Object.values(poll.data?.proxies ?? {}).sort((x, y) =>
			String(x.node_ip ?? '').localeCompare(String(y.node_ip ?? ''), undefined, { numeric: true }),
		),
	);

	// ── helpers ─── mirrored from routes/serve/+page.svelte: the same states must wear the same
	// colours, and the same numbers must be formatted the same way, here.
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

	function select(node: HTMLElement, name: string, route: string | null) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-compute-serve',
					kind: 'serve-app',
					id: name,
					label: route ?? name,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if poll.error !== null}
		<p class="text-destructive text-sm">Serve unreachable: {poll.error}</p>
	{:else if apps.length === 0}
		<p class="text-muted-foreground text-sm">No Serve applications deployed.</p>
	{:else}
		<div class="flex flex-col gap-4 text-sm">
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
									<th class="px-3 py-2">status</th>
									<th class="px-3 py-2">node ip</th>
									<th class="px-3 py-2">pod</th>
									<th class="px-3 py-2">log</th>
								</tr>
							</thead>
							<tbody>
								{#each proxies as p (p.node_id)}
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
				<Card
					class="cursor-pointer overflow-hidden border-l-2 {accent(app.status)}"
					onclick={(e) => select(e.currentTarget, app.name, app.route_prefix ?? null)}
				>
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
								{#if app.source}<span class="text-muted-foreground text-[10px] tracking-wide uppercase">{app.source}</span>{/if}
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
								<span class="text-muted-foreground/60">· deployed {fmtAgo(app.last_deployed_time_s)}</span>
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
									{#if d.status_trigger}<span class="text-muted-foreground/70 text-[10px]">{d.status_trigger}</span>{/if}

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
										<span class="text-muted-foreground font-mono text-xs tabular-nums">{live}/{target}</span>
									</div>

									{#if resources.length}
										<div class="flex w-full flex-wrap gap-1 sm:w-auto">
											{#each resources as [k, v] (k)}
												<span class="rounded-md px-1.5 py-0.5 text-[10px] font-medium {resourceChip(k)}"
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
											<ChevronRight class="h-3 w-3 transition-transform group-open:rotate-90" /> Replicas ({live})
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
																><Badge variant={replicaState(r.state)} class="scale-90">{r.state}</Badge></td
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
										<div class="space-y-2 px-4 pt-1 pb-3">
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
														<span class="bg-muted rounded px-1.5 py-0.5 font-mono text-[10px]">{p}</span>
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
		</div>
	{/if}
</div>
