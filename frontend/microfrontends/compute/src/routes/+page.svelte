<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		getOverview,
		getRayCluster,
		getRayJobs,
		getActors,
		getTasks,
		getServe,
	} from '$lib/remote/compute.remote';
	import RayStatus from '$lib/components/ray-status.svelte';
	import { Card } from '@rask/ui/card';
	import {
		Server,
		Cpu,
		ListTree,
		Boxes,
		ServerCog,
		ListChecks,
		Info,
		TriangleAlert,
		CircleAlert,
	} from '@lucide/svelte';

	// THE ONE PATTERN (see lib/remote/compute.remote.ts): every read is a cached
	// remote query, polled below via `.refresh().catch()`, read imperatively
	// (`.current`) for flicker-free refresh. All six dashboard proxies are
	// offline-safe (never 5xx), so each resolves even when Ray is down.
	const overviewQuery = getOverview();
	const clusterQuery = getRayCluster();
	const jobsQuery = getRayJobs();
	const actorsQuery = getActors();
	const tasksQuery = getTasks();
	const serveQuery = getServe();

	const ov = $derived(overviewQuery.current ?? null);
	const cluster = $derived(clusterQuery.current ?? null);
	const jobs = $derived(jobsQuery.current ?? null);
	const actors = $derived(actorsQuery.current ?? []);
	const taskRows = $derived(tasksQuery.current ?? []);
	const serve = $derived(serveQuery.current ?? null);
	// Mirror the original: surface only the overview transport failure.
	const error = $derived(overviewQuery.error ? String(overviewQuery.error) : null);

	// Poll Ray every 5s. `.catch(() => {})` is mandatory: an uncaught refresh
	// rejection (one 500) evicts the query from cache and kills the loop. Each
	// query refreshes independently so one failure can't stop the others.
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

	const jobList = $derived(jobs?.jobs ?? []);
	const serveApps = $derived(Object.values(serve?.applications ?? {}));

	const cards = $derived([
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

	function sevVariant(s: string) {
		if (s === 'ERROR') return { icon: CircleAlert, cls: 'text-destructive' };
		if (s === 'WARNING') return { icon: TriangleAlert, cls: 'text-amber-600 dark:text-amber-400' };
		return { icon: Info, cls: 'text-muted-foreground' };
	}
</script>

<svelte:head>
	<title>Overview — RASK</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<div class="flex flex-col gap-4 p-6 text-sm">
		<!-- Live Ray cluster signal — top of the overview (the cluster's "is it up?"). -->
		<div class="flex items-center">
			<div class="bg-card inline-flex items-center rounded-full border px-3 py-1.5">
				<RayStatus />
			</div>
		</div>

		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 text-destructive p-3">{error}</Card>
		{/if}

		<!-- Summary cards -->
		<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
			{#each cards as c (c.label)}
				<a href={c.href} class="block">
					<Card class="hover:border-primary/50 relative overflow-hidden p-4 transition-colors">
						<div class="absolute inset-x-0 top-0 h-0.5 {c.dot}"></div>
						<div
							class="text-muted-foreground flex items-center gap-1.5 text-[11px] font-medium tracking-wide uppercase"
						>
							<c.icon class="h-3.5 w-3.5" />{c.label}
						</div>
						<div class="mt-1 font-mono text-2xl tabular-nums">
							{c.value}{#if c.total !== null}<span class="text-muted-foreground text-base"
									>/{c.total}</span
								>{/if}
						</div>
					</Card>
				</a>
			{/each}
		</section>

		<!-- Events -->
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
			<div class="max-h-[55vh] divide-y overflow-auto">
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
