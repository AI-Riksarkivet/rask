<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		rayOverview,
		rayCluster,
		rayJobs,
		actorsList,
		tasksList,
		serveApplications,
		type OverviewPayload,
		type RayClusterPayload,
		type RayJobsPayload,
		type ActorInfo,
		type TaskInfo,
		type ServePayload,
	} from '@rask/api';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
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
	} from 'lucide-svelte';

	let ov = $state<OverviewPayload | null>(null);
	let cluster = $state<RayClusterPayload | null>(null);
	let jobs = $state<RayJobsPayload | null>(null);
	let actors = $state<ActorInfo[]>([]);
	let taskRows = $state<TaskInfo[]>([]);
	let serve = $state<ServePayload | null>(null);
	let error = $state<string | null>(null);
	let timer: ReturnType<typeof setInterval> | null = null;

	async function refresh() {
		const [o, c, j, a, t, s] = await Promise.allSettled([
			rayOverview(),
			rayCluster(),
			rayJobs(),
			actorsList(),
			tasksList(),
			serveApplications(),
		]);
		if (o.status === 'fulfilled') ov = o.value;
		if (c.status === 'fulfilled') cluster = c.value;
		if (j.status === 'fulfilled') jobs = j.value;
		if (a.status === 'fulfilled') actors = a.value;
		if (t.status === 'fulfilled') taskRows = t.value;
		if (s.status === 'fulfilled') serve = s.value;
		error = o.status === 'rejected' ? (o.reason?.message ?? 'overview unavailable') : null;
	}

	onMount(() => {
		refresh();
		timer = setInterval(refresh, 5000);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	const jobList = $derived(jobs?.jobs ?? []);
	const serveApps = $derived(Object.values(serve?.applications ?? {}));

	const cards = $derived([
		{
			href: '/cluster',
			label: 'Nodes',
			icon: Server,
			value: cluster?.alive_count ?? 0,
			total: cluster?.node_count ?? null,
			dot: 'bg-emerald-500',
		},
		{
			href: '/cluster',
			label: 'GPU',
			icon: Cpu,
			value: Math.round(cluster?.used_resources?.GPU ?? 0),
			total: Math.round(cluster?.total_resources?.GPU ?? 0) || null,
			dot: 'bg-violet-500',
		},
		{
			href: '/jobs',
			label: 'Jobs running',
			icon: ListTree,
			value: jobList.filter((j) => j.status === 'RUNNING').length,
			total: jobList.length || null,
			dot: 'bg-sky-500',
		},
		{
			href: '/jobs',
			label: 'Tasks running',
			icon: ListChecks,
			value: taskRows.filter((t) => t.state === 'RUNNING').length,
			total: taskRows.length || null,
			dot: 'bg-amber-500',
		},
		{
			href: '/actors',
			label: 'Actors alive',
			icon: Boxes,
			value: actors.filter((a) => a.state === 'ALIVE').length,
			total: actors.length || null,
			dot: 'bg-fuchsia-500',
		},
		{
			href: '/serve',
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

<RayShell title="Overview">
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
</RayShell>
