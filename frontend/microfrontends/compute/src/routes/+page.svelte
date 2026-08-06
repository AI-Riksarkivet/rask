<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import {
		getActors,
		getOverview,
		getRayCluster,
		getRayJobs,
		getServe,
		getTasks,
	} from '$lib/remote/compute.remote';
	import { lineageFeed, type RunNotice } from '$lib/live/feeds.remote';
	import RayStatus from '$lib/components/ray-status.svelte';
	import { Card } from '@rask/ui/card';
	import { Badge, type BadgeVariant } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import {
		Boxes,
		CircleAlert,
		Cpu,
		Info,
		ListChecks,
		ListTree,
		Server,
		ServerCog,
		TriangleAlert,
	} from '@lucide/svelte';

	// The compute landing — the Ray-plane summary merged with the PIPELINE RUNS feed. The
	// batches/HTR dashboard died at P7a (the batches table + orchestrator plane are gone):
	// ingestion is `POST /api/ingest` (the /new page) and the cascade's progress is the
	// lineage runs feed — the same `query.live` pulse that backs the shell's bell, so the
	// landing and the bell can never disagree about the estate.
	//
	// THE ONE PATTERN (see lib/remote/compute.remote.ts):
	//  - Live Ray reads (events/cluster/jobs/actors/tasks/serve) are the SAME query objects the
	//    other compute views use, polled below via `.refresh().catch()` and read imperatively
	//    (`.current`) so refresh is flicker-free and a single 500 can't kill the poll loop.
	//  - The runs panel rides `lineageFeed` (query.live) — change-driven, no poll here.
	const overviewQuery = getOverview();
	const clusterQuery = getRayCluster();
	const jobsQuery = getRayJobs();
	const actorsQuery = getActors();
	const tasksQuery = getTasks();
	const serveQuery = getServe();
	const runsQuery = lineageFeed();

	// Polled Ray reads — imperative `.current` (undefined until first resolve). All dashboard
	// proxies are offline-safe (never 5xx), so each resolves even when Ray is down.
	const ov = $derived(overviewQuery.current ?? null);
	const cluster = $derived(clusterQuery.current ?? null);
	const jobsPayload = $derived(jobsQuery.current ?? null);
	const actors = $derived(actorsQuery.current ?? []);
	const taskRows = $derived(tasksQuery.current ?? []);
	const serve = $derived(serveQuery.current ?? null);
	const runs = $derived(runsQuery.current?.runs ?? []);

	// POLL REASON: the Ray dashboard REST API is snapshot-only introspection — Ray publishes no
	// change events a cursor could ride, so re-reading cluster/job/actor/task/serve/event state on
	// a clock is the only transport. The runs feed is NOT polled — it's a live query driven by the
	// lineage cursor. `.catch(() => {})` is mandatory: an uncaught refresh rejection (one 500)
	// evicts the query from cache and kills the loop. Each query refreshes independently.
	onMount(() => {
		const timer = setInterval(() => {
			overviewQuery.refresh().catch(() => {});
			clusterQuery.refresh().catch(() => {});
			jobsQuery.refresh().catch(() => {});
			actorsQuery.refresh().catch(() => {});
			tasksQuery.refresh().catch(() => {});
			serveQuery.refresh().catch(() => {});
		}, 5000);
		return () => clearInterval(timer);
	});

	const jobList = $derived(jobsPayload?.jobs ?? []);
	const serveApps = $derived(Object.values(serve?.applications ?? {}));

	// The zone's live counts — each card soft-navs into the view that explains it.
	const clusterCards = $derived([
		{
			href: `${base}/cluster`,
			label: 'Nodes',
			icon: Server,
			value: cluster?.alive_count ?? 0,
			total: cluster?.node_count ?? null,
			dot: 'bg-emerald-500',
		},
		{
			href: `${base}/cluster`,
			label: 'GPU',
			icon: Cpu,
			value: Math.round(cluster?.used_resources?.GPU ?? 0),
			total: Math.round(cluster?.total_resources?.GPU ?? 0) || null,
			dot: 'bg-violet-500',
		},
		{
			href: `${base}/jobs`,
			label: 'Jobs running',
			icon: ListTree,
			value: jobList.filter((j) => j.status === 'RUNNING').length,
			total: jobList.length || null,
			dot: 'bg-sky-500',
		},
		{
			href: `${base}/jobs`,
			label: 'Tasks running',
			icon: ListChecks,
			value: taskRows.filter((t) => t.state === 'RUNNING').length,
			total: taskRows.length || null,
			dot: 'bg-amber-500',
		},
		{
			href: `${base}/actors`,
			label: 'Actors alive',
			icon: Boxes,
			value: actors.filter((a) => a.state === 'ALIVE').length,
			total: actors.length || null,
			dot: 'bg-fuchsia-500',
		},
		{
			href: `${base}/serve`,
			label: 'Serve apps',
			icon: ServerCog,
			value: serveApps.filter((a) => a.status === 'RUNNING').length,
			total: serveApps.length || null,
			dot: 'bg-teal-500',
		},
	]);

	function jobBadgeVariant(s: string): BadgeVariant {
		switch (s) {
			case 'SUCCEEDED':
				return 'success';
			case 'RUNNING':
			case 'PENDING':
				return 'warning';
			case 'FAILED':
				return 'destructive';
			default:
				return 'secondary';
		}
	}

	function runBadgeVariant(state: string | null): BadgeVariant {
		switch ((state ?? '').toUpperCase()) {
			case 'COMPLETE':
				return 'success';
			case 'RUNNING':
			case 'START':
				return 'warning';
			case 'FAIL':
			case 'ABORT':
				return 'destructive';
			default:
				return 'secondary';
		}
	}

	function runProgress(r: RunNotice): number | null {
		if (r.progress_total == null || r.progress_done == null || r.progress_total === 0) return null;
		return (r.progress_done / r.progress_total) * 100;
	}

	function sevVariant(s: string) {
		if (s === 'ERROR') return { icon: CircleAlert, cls: 'text-destructive' };
		if (s === 'WARNING') return { icon: TriangleAlert, cls: 'text-amber-600 dark:text-amber-400' };
		return { icon: Info, cls: 'text-muted-foreground' };
	}

	function fmtRuntime(start: number | null, end: number | null): string {
		if (!start) return '—';
		const endMs = end ?? Date.now();
		const secs = Math.max(0, (endMs - start) / 1000);
		if (secs < 90) return `${secs.toFixed(0)}s`;
		if (secs < 5400) return `${(secs / 60).toFixed(1)}m`;
		return `${(secs / 3600).toFixed(1)}h`;
	}

	function fmtWhen(iso: string | null): string {
		if (!iso) return '—';
		const d = new Date(iso);
		const sameDay = d.toDateString() === new Date().toDateString();
		return sameDay
			? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
			: d.toLocaleString([], {
					month: 'short',
					day: 'numeric',
					hour: '2-digit',
					minute: '2-digit',
				});
	}
</script>

<svelte:head>
	<title>Compute — RASK</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<div class="flex flex-col gap-4 p-6 text-sm">
		<!-- Live Ray cluster signal + the one action: ingest a volume (the P7a pipeline head). -->
		<div class="flex items-center justify-between gap-2">
			<div class="bg-card inline-flex items-center rounded-full border px-3 py-1.5">
				<RayStatus />
			</div>
			<!-- `/etl`, not `/new`. The route was renamed and this button was not, so the landing page's
			     only path into the ingest plane 404'd. Invisible because nothing gates a dead INTERNAL
			     link — the cross-zone gate only checks that cross-zone anchors carry `data-sveltekit-reload`.
			     "a source", not "volume": a volume is one source kind's unit, and the door takes any
			     registered kind. -->
			<Button size="sm" onclick={() => goto(`${base}/etl`)}>Ingest a source</Button>
		</div>

		<!-- Live cluster counts — each card soft-navs into its view -->
		<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
			{#each clusterCards as c (c.label)}
				<a href={c.href} class="block">
					<Card class="hover:border-primary/50 relative overflow-hidden p-4 transition-colors">
						<div class="absolute inset-x-0 top-0 h-0.5 {c.dot}"></div>
						<div
							class="text-muted-foreground flex items-center gap-1.5 text-[11px] font-medium tracking-wide uppercase"
						>
							<c.icon class="h-3.5 w-3.5" />{c.label}
						</div>
						<div class="mt-1 font-mono text-2xl tabular-nums">
							{c.value}{#if c.total !== null}<span class="text-muted-foreground text-base">/{c.total}</span>{/if}
						</div>
					</Card>
				</a>
			{/each}
		</section>

		<!-- Pipeline runs — the lineage runs feed (failures first, the same pulse as the bell).
		     This replaced the P7a-retired batches/chunks dashboard: cascade progress IS run state. -->
		<Card class="overflow-hidden">
			<div class="flex items-center justify-between border-b px-4 py-2">
				<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
					Pipeline runs
				</div>
				<span class="text-muted-foreground text-[11px]">failures first · live</span>
			</div>
			{#if !runs.length}
				<div class="text-muted-foreground px-4 py-6 text-center">
					No runs yet — ingest a volume to start the cascade.
				</div>
			{:else}
				<div class="divide-border flex max-h-72 flex-col divide-y overflow-auto">
					{#each runs as r (r.run_id)}
						{@const pctVal = runProgress(r)}
						<div class="flex items-center gap-3 px-4 py-1.5 text-xs">
							<Badge
								variant={runBadgeVariant(r.state)}
								class={`min-w-[72px] justify-center ${(r.state ?? '').toUpperCase() === 'RUNNING' ? 'animate-pulse' : ''}`}
							>
								{r.state ?? '—'}
							</Badge>
							<span class="text-foreground truncate font-mono" title={r.run_id}>
								{r.operation ?? r.job ?? r.run_id.slice(0, 12)}
							</span>
							{#if r.author}
								<span class="text-muted-foreground">by {r.author}</span>
							{/if}
							{#if pctVal !== null}
								<div class="bg-muted h-1.5 w-28 overflow-hidden rounded-full">
									<div
										class="h-full bg-emerald-500 transition-all"
										style:width={`${pctVal}%`}
									></div>
								</div>
								<span class="text-muted-foreground font-mono tabular-nums">
									{r.progress_done}/{r.progress_total}
								</span>
							{/if}
							{#if r.error_message}
								<span class="text-destructive truncate" title={r.error_message}>
									{r.error_message}
								</span>
							{/if}
							<span class="text-muted-foreground ml-auto shrink-0">{fmtWhen(r.updated_at)}</span>
						</div>
					{/each}
				</div>
			{/if}
		</Card>

		<!-- Recent RayJobs -->
		{#if jobsPayload?.ok && jobList.length}
			<Card class="overflow-hidden">
				<div class="flex items-center justify-between border-b px-4 py-2">
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						Recent RayJobs
					</div>
					<a class="text-primary text-xs hover:underline" href={`${base}/jobs`}>view all →</a>
				</div>
				<div class="divide-border flex max-h-44 flex-col divide-y overflow-auto">
					{#each jobList.slice(0, 10) as j (j.submission_id ?? j.job_id)}
						<div class="flex items-center gap-3 px-4 py-1.5 text-xs">
							<Badge
								variant={jobBadgeVariant(j.status)}
								class={`min-w-[68px] justify-center ${j.status === 'RUNNING' ? 'animate-pulse' : ''}`}
							>
								{j.status}
							</Badge>
							<span class="text-foreground font-mono">{(j.submission_id ?? '—').slice(0, 24)}</span>
							<span class="text-muted-foreground">{fmtRuntime(j.start_time, j.end_time)}</span>
							{#if j.submission_id}
								<a
									class="text-primary ml-auto hover:underline"
									href={`${base}/jobs/${encodeURIComponent(j.submission_id)}`}
									title="Open job detail">details</a
								>
							{/if}
						</div>
					{/each}
				</div>
			</Card>
		{/if}

		<!-- Cluster events -->
		<Card class="overflow-hidden">
			<div
				class="text-muted-foreground flex items-center justify-between border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
			>
				<span>Cluster events</span>
				{#if ov?.ray_version}
					<span class="normal-case">
						Ray {ov.ray_version}
						{#if ov.session_name}· <span class="font-mono">{ov.session_name}</span>{/if}
					</span>
				{/if}
			</div>
			{#if ov && !ov.events.length}
				<div class="text-muted-foreground px-4 py-6 text-center">No recent events.</div>
			{/if}
			<div class="max-h-[40vh] divide-y overflow-auto">
				{#each ov?.events ?? [] as e (e.event_id)}
					{@const sev = sevVariant(e.severity)}
					<div class="flex items-start gap-2.5 px-4 py-2">
						<sev.icon class="mt-0.5 h-3.5 w-3.5 shrink-0 {sev.cls}" />
						<div class="min-w-0 flex-1">
							<div class="break-words">{e.message}</div>
							<div class="text-muted-foreground mt-0.5 flex gap-2 font-mono text-[10px]">
								<span>{e.time ?? ''}</span>
								{#if e.source_type}<span class="bg-muted rounded px-1">{e.source_type}</span>{/if}
							</div>
						</div>
					</div>
				{/each}
			</div>
		</Card>

		{#if ov?.dashboard_url}
			<div class="text-muted-foreground text-xs">
				dashboard: <a
					class="text-primary hover:underline"
					href={ov.dashboard_url}
					target="_blank"
					rel="noopener">{ov.dashboard_url} ↗</a
				>
			</div>
		{/if}
	</div>
</main>
