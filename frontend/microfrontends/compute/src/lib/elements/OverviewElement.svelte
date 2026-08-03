<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-compute-overview>` — the compute landing's RAY-BACKED half, VISUALLY IDENTICAL to the
	 * zone's /compute page: the same Ray health pill, the same six live count cards (same accent
	 * bars, same lucide icons, same value/total typography), the same "Recent RayJobs" list and the
	 * same "Cluster events" card with its Ray version / session line and dashboard footer — same
	 * Badge variants, same utility classes (compiled into this bundle by elements.css, since the
	 * host page's Tailwind cannot generate them). The mount stamp + poll counter stay as the
	 * no-remount witness; the count cards and the job rows dispatch the rask:select contract event
	 * instead of navigating.
	 *
	 * OMITTED ON PURPOSE — the page's "Pipeline runs" feed. It rides `lineageFeed` (`query.live` in
	 * lib/live/feeds.remote.ts), which reaches the lineage service through the zone's PRIVATE env
	 * (`LINEAGE_API`, `LINEAGE_SERVICE_TOKEN`) server-side. A custom element has no remote-function
	 * endpoint and no root-absolute route that answers for it, so the feed is not reachable from
	 * here and a runs list is NOT faked. The workbench already lends `rask-lakehouse-runs` for
	 * exactly that surface — pair the two panels. Page chrome is likewise skipped: the "Ingest
	 * volume" button (goto) and the cards' `href` navigation.
	 *
	 * THE READS are the same `/api/ray/*` proxies the page's remote functions wrap, taken directly
	 * through @rask/api. They are settled INDEPENDENTLY (Promise.allSettled), mirroring the page's
	 * per-query `.refresh().catch(() => {})`: one dead read leaves the other cards honest, and only
	 * a total blackout raises the panel-level error.
	 */
	import { Badge, type BadgeVariant } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import {
		actorsList,
		rayCluster,
		rayHealth,
		rayJobs,
		rayOverview,
		serveApplications,
		tasksList,
		type ActorInfo,
		type OverviewPayload,
		type RayClusterPayload,
		type RayHealth,
		type RayJob,
		type RayJobsPayload,
		type ServePayload,
		type TaskInfo,
	} from '@rask/api';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { RayPoll } from './ray-poll.svelte';
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

	interface OverviewSnapshot {
		health: RayHealth | null;
		overview: OverviewPayload | null;
		cluster: RayClusterPayload | null;
		jobs: RayJobsPayload | null;
		actors: ActorInfo[] | null;
		tasks: TaskInfo[] | null;
		serve: ServePayload | null;
	}

	function settled<T>(r: PromiseSettledResult<T>): T | null {
		return r.status === 'fulfilled' ? r.value : null;
	}

	async function readAll(f: typeof fetch): Promise<OverviewSnapshot> {
		const results = await Promise.allSettled([
			rayHealth(f),
			rayOverview(f),
			rayCluster(f),
			rayJobs(f),
			actorsList(f),
			tasksList(f),
			serveApplications(f),
		]);
		const rejected = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected');
		// Only a TOTAL blackout is a panel error — anything less renders honestly per card.
		if (rejected.length === results.length) {
			throw new Error(String(rejected[0]?.reason ?? 'no Ray read resolved'));
		}
		const [health, overview, cluster, jobs, actors, tasks, serve] = results;
		return {
			health: settled(health),
			overview: settled(overview),
			cluster: settled(cluster),
			jobs: settled(jobs),
			actors: settled(actors),
			tasks: settled(tasks),
			serve: settled(serve),
		};
	}

	let { pollms = 5000 }: { pollms?: number } = $props();
	const poll = new RayPoll<OverviewSnapshot>((f) => readAll(f));
	$effect(() => poll.start(pollms));

	const health = $derived(poll.data?.health ?? null);
	const ov = $derived(poll.data?.overview ?? null);
	const cluster = $derived(poll.data?.cluster ?? null);
	const jobsPayload = $derived(poll.data?.jobs ?? null);
	const actors = $derived(poll.data?.actors ?? []);
	const taskRows = $derived(poll.data?.tasks ?? []);
	const serve = $derived(poll.data?.serve ?? null);

	const jobList = $derived(jobsPayload?.jobs ?? []);
	const serveApps = $derived(Object.values(serve?.applications ?? {}));

	// The zone's live counts — mirrored from routes/+page.svelte, minus the href (a panel selects,
	// it does not navigate).
	const clusterCards = $derived([
		{
			key: 'nodes',
			label: 'Nodes',
			icon: Server,
			value: cluster?.alive_count ?? 0,
			total: cluster?.node_count ?? null,
			dot: 'bg-emerald-500',
		},
		{
			key: 'gpu',
			label: 'GPU',
			icon: Cpu,
			value: Math.round(cluster?.used_resources?.GPU ?? 0),
			total: Math.round(cluster?.total_resources?.GPU ?? 0) || null,
			dot: 'bg-violet-500',
		},
		{
			key: 'jobs-running',
			label: 'Jobs running',
			icon: ListTree,
			value: jobList.filter((j) => j.status === 'RUNNING').length,
			total: jobList.length || null,
			dot: 'bg-sky-500',
		},
		{
			key: 'tasks-running',
			label: 'Tasks running',
			icon: ListChecks,
			value: taskRows.filter((t) => t.state === 'RUNNING').length,
			total: taskRows.length || null,
			dot: 'bg-amber-500',
		},
		{
			key: 'actors-alive',
			label: 'Actors alive',
			icon: Boxes,
			value: actors.filter((a) => a.state === 'ALIVE').length,
			total: actors.length || null,
			dot: 'bg-fuchsia-500',
		},
		{
			key: 'serve-apps',
			label: 'Serve apps',
			icon: ServerCog,
			value: serveApps.filter((a) => a.status === 'RUNNING').length,
			total: serveApps.length || null,
			dot: 'bg-teal-500',
		},
	]);

	// Mirrored from routes/+page.svelte — the same states must wear the same colours here, and the
	// same numbers must be formatted the same way.
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

	function selectMetric(node: HTMLElement, key: string, label: string) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-compute-overview',
					kind: 'compute-metric',
					id: key,
					label,
				} satisfies SelectDetail,
			}),
		);
	}

	function selectJob(node: HTMLElement, job: RayJob) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-compute-overview',
					kind: 'ray-job',
					id: job.submission_id,
					label: job.entrypoint ?? job.submission_id,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if poll.error !== null}
		<p class="text-destructive text-sm">Ray unreachable: {poll.error}</p>
	{:else}
		<div class="flex flex-col gap-4 text-sm">
			<!-- Live Ray cluster signal (the page's RayStatus pill; the "Ingest volume" action is
			     navigation, so it stays with the page). -->
			<div class="flex items-center justify-between gap-2">
				<div class="bg-card inline-flex items-center rounded-full border px-3 py-1.5">
					<div class="flex items-center gap-2 text-xs" title={health?.error ?? ''}>
						<span
							class="size-1.5 shrink-0 rounded-full {health?.ok
								? 'bg-emerald-500'
								: 'bg-amber-500'}"
						></span>
						<span class="text-muted-foreground truncate">
							{#if health?.ok}
								Ray {health.ray_version ?? 'connected'}
							{:else}
								Ray offline
							{/if}
						</span>
					</div>
				</div>
			</div>

			<!-- Live cluster counts — each card selects instead of navigating -->
			<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
				{#each clusterCards as c (c.label)}
					<button
						type="button"
						class="block w-full text-left"
						onclick={(e) => selectMetric(e.currentTarget, c.key, c.label)}
					>
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
					</button>
				{/each}
			</section>

			<!-- Recent RayJobs -->
			{#if jobsPayload?.ok && jobList.length}
				<Card class="overflow-hidden">
					<div class="flex items-center justify-between border-b px-4 py-2">
						<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
							Recent RayJobs
						</div>
						<span class="text-muted-foreground text-[11px]">{jobList.length} total</span>
					</div>
					<div class="divide-border flex max-h-44 flex-col divide-y overflow-auto">
						{#each jobList.slice(0, 10) as j (j.submission_id ?? j.job_id)}
							<button
								type="button"
								class="hover:bg-muted/40 flex w-full cursor-pointer items-center gap-3 px-4 py-1.5 text-left text-xs"
								onclick={(e) => selectJob(e.currentTarget, j)}
							>
								<Badge
									variant={jobBadgeVariant(j.status)}
									class={`min-w-[68px] justify-center ${j.status === 'RUNNING' ? 'animate-pulse' : ''}`}
								>
									{j.status}
								</Badge>
								<span class="text-foreground font-mono">{(j.submission_id ?? '—').slice(0, 24)}</span>
								<span class="text-muted-foreground">{fmtRuntime(j.start_time, j.end_time)}</span>
								<span class="text-muted-foreground ml-auto truncate font-mono">{j.entrypoint ?? '—'}</span>
							</button>
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
				{#if ov === null}
					<div class="text-muted-foreground px-4 py-6 text-center">Events unavailable.</div>
				{:else if !ov.events.length}
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
	{/if}
</div>
