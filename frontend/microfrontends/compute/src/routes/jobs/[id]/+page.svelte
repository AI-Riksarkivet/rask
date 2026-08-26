<script lang="ts">
	import { liveRead } from '@rask/api/live';
	import { rayClock } from '$lib/live/ray-clock.svelte';
	import { page } from '$app/state';
	import { base } from '$app/paths';
	import { type TaskInfo } from '@rask/api';
	import {
		getRayJobs,
		getJobTasks,
		getRayCluster,
		getRayJobLogs,
	} from '$lib/remote/compute.remote';
	import { Card } from '@rask/ui/card';
	import { Badge, type BadgeVariant } from '@rask/ui/badge';
	import { SortHeader } from '@rask/ui/sort-header';
	import {
		ArrowLeft,
		TriangleAlert,
		Info,
		FileText,
		ChevronRight,
		RefreshCw,
	} from '@lucide/svelte';

	// THE ONE PATTERN (see lib/remote/compute.remote.ts): the three dashboards
	// (jobs/tasks/cluster) are cached remote queries read imperatively
	// (`.current`) and polled below. The driver logs are a PARAM query keyed by
	// `{ id }` so navigating the submission id re-keys it; extract `id` to a
	// `$derived` first so the logs query re-runs on navigation.
	const id = $derived(decodeURIComponent(page.params.id ?? ''));

	const jobsQuery = getRayJobs();
	const clusterQuery = getRayCluster();
	// Param query — re-keys when `id` changes (navigation between job details).
	const logsQuery = $derived(getRayJobLogs({ id }));

	const jobs = $derived(jobsQuery.current?.jobs ?? []);
	const nodes = $derived(clusterQuery.current?.nodes ?? []);
	const logsPayload = $derived(logsQuery.current ?? null);
	const logText = $derived(logsPayload?.ok ? logsPayload.logs : '');
	const logsLoading = $derived(logsQuery.loading);
	// `loaded` once the jobs list has resolved at least once (drives the
	// "no such job" empty state, which must not flash before the first load).
	const loaded = $derived(jobsQuery.ready);
	const error = $derived(jobsQuery.error ? String(jobsQuery.error) : null);

	let logPre: HTMLElement | null = $state(null);
	let logStick = $state(true);

	let sortKey = $state('started');
	let sortDir = $state<'asc' | 'desc'>('desc');
	function setSort(col: string) {
		if (sortKey === col) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		else {
			sortKey = col;
			sortDir = col === 'started' || col === 'duration' ? 'desc' : 'asc';
		}
	}

	const job = $derived(jobs.find((j) => j.submission_id === id) ?? null);
	// Ray omits `metadata` entirely for a job submitted without any, so every read is optional —
	// `@rask/api`'s ray schema made that field REQUIRED once and a single metadata-less job blanked
	// the whole board.
	const transformName = $derived(job?.metadata?.['rask.transform'] ?? '');
	const stageName = $derived(job?.metadata?.['rask.stage'] ?? '');
	// Param query — created once the job payload names its Ray job_id; server-filtered (#140).
	const tasksQuery = $derived(job?.job_id ? getJobTasks({ jobId: job.job_id }) : null);
	const tasks = $derived(tasksQuery?.current ?? []);
	const running = $derived(job?.status === 'RUNNING' || job?.status === 'PENDING');

	// Manual log refresh (toolbar button) — event handler, so `.refresh()` (one
	// of the always-callable methods) not `.current`.
	//
	// NOT `rayClock.refresh()`, deliberately: that coalesces per tick, and a person pressing the
	// button within five seconds of the automatic read would get nothing. A manual refresh means
	// "ask NOW" — it is the one call that must never be de-duplicated.
	function refreshLogs() {
		logsQuery.refresh().catch(() => {});
	}

	// BOUNDED, and the bound is the point. This page used to say "the rest always refresh
	// independently" as though it were a feature; it was the defect. A tab left open on a SUCCEEDED
	// job kept re-reading `getRayJobs()` — the heaviest call in `ray-kit`, the one with the
	// 81,155-job / 164.7 MB history behind it — every five seconds, forever, one directory away from
	// `ingest/[run_id]` whose own comment forbids exactly that ("polling a finished run forever is how
	// a status page becomes the busiest client of the service it reports on"). Two pages in one zone
	// taught contradictory rules; they now teach the same one.
	//
	// Ray still has no publisher — that reason is intact and lives with the clock in
	// `$lib/live/ray-clock`. What is added here is the STOP: with no subscriber the shared clock does
	// not tick at all, so a terminal job issues zero requests rather than merely fewer.
	const terminal = $derived(job !== null && !running);
	$effect(() => {
		if (terminal) return;
		return rayClock.subscribe();
	});
	liveRead(
		() => rayClock.cursor,
		() => {
			rayClock.refresh(jobsQuery);
			if (tasksQuery) rayClock.refresh(tasksQuery);
			rayClock.refresh(clusterQuery);
			// A live job's log file grows with no event that says it grew — and a terminal job's does
			// not grow at all, so this guard is a second, independent bound on the heaviest read here.
			if (running) logsQuery.refresh().catch(() => {});
		},
	);

	// Tail behaviour for the log pane.
	function onLogScroll() {
		if (logPre) logStick = logPre.scrollHeight - logPre.scrollTop - logPre.clientHeight < 40;
	}
	$effect(() => {
		logText;
		if (logPre && logStick) logPre.scrollTop = logPre.scrollHeight;
	});

	const nodeMap = $derived(new Map(nodes.map((n) => [n.node_id, n])));
	const jobTasks = $derived(tasks); // already narrowed server-side
	// The FAILURE signal, kept apart from the mere presence of `message` — Ray sets that on every
	// terminal job, success included.
	const jobFailed = $derived(
		Boolean(job?.error_type) || job?.status === 'FAILED' || job?.status === 'STOPPED',
	);

	const sortedTasks = $derived.by(() => {
		const dir = sortDir === 'asc' ? 1 : -1;
		return [...jobTasks].sort((x, y) => {
			const a = taskVal(x, sortKey);
			const b = taskVal(y, sortKey);
			if (a == null && b == null) return 0;
			if (a == null) return 1;
			if (b == null) return -1;
			const c =
				typeof a === 'number' && typeof b === 'number'
					? a - b
					: String(a).localeCompare(String(b), undefined, { numeric: true });
			return c * dir;
		});
	});

	// ── helpers ──
	function jobVariant(s: string): BadgeVariant {
		if (s === 'SUCCEEDED') return 'success';
		if (s === 'RUNNING' || s === 'PENDING') return 'warning';
		if (s === 'FAILED') return 'destructive';
		return 'secondary';
	}
	function taskStateVariant(s: string): 'success' | 'secondary' | 'destructive' {
		if (s === 'FINISHED') return 'success';
		if (s === 'FAILED') return 'destructive';
		return 'secondary';
	}
	const typeLabel = (t: string | null) =>
		(t ?? '').replace('ACTOR_CREATION_TASK', 'actor-init').replace('_TASK', '').toLowerCase() ||
		'—';
	const short = (s: string | null | undefined) =>
		(s ?? '').replace(/^kuberay-ai-dev-cluster-/, '') || '';
	function nodeLabel(nid: string | null): string {
		if (!nid) return '—';
		const n = nodeMap.get(nid);
		return short(n?.hostname) || n?.node_ip || nid.slice(0, 12);
	}
	function taskStart(t: TaskInfo): number | null {
		return t.start_time_ms ?? t.creation_time_ms;
	}
	function taskDuration(t: TaskInfo): number | null {
		const s = taskStart(t);
		if (!s) return null;
		return ((t.end_time_ms ?? Date.now()) - s) / 1000;
	}
	function taskVal(t: TaskInfo, key: string): string | number | null {
		switch (key) {
			case 'state':
				return t.state;
			case 'type':
				return t.type;
			case 'task':
				return t.func_or_class_name ?? t.name;
			case 'node':
				return nodeLabel(t.node_id);
			case 'pid':
				return t.worker_pid;
			case 'started':
				return taskStart(t);
			case 'duration':
				return taskDuration(t);
			default:
				return null;
		}
	}
	function fmtTime(ts: number | null): string {
		if (!ts) return '—';
		return new Date(ts).toISOString().replace('T', ' ').slice(0, 19);
	}
	function fmtDur(secs: number | null): string {
		if (secs == null) return '—';
		secs = Math.max(0, secs);
		if (secs < 1) return `${(secs * 1000).toFixed(0)}ms`;
		if (secs < 90) return `${secs.toFixed(1)}s`;
		if (secs < 5400) return `${(secs / 60).toFixed(1)}m`;
		return `${(secs / 3600).toFixed(1)}h`;
	}
	function fmtAgo(ms: number | null): string {
		if (!ms) return '—';
		const s = Math.max(0, Date.now() / 1000 - ms / 1000);
		if (s < 60) return `${Math.floor(s)}s ago`;
		if (s < 3600) return `${Math.floor(s / 60)}m ago`;
		if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
		return `${Math.floor(s / 86400)}d ago`;
	}
	function logLineClass(t: string): string {
		if (/\b(ERROR|CRITICAL|FATAL)\b|Traceback|Exception|Error:/.test(t)) return 'text-destructive';
		if (/\bWARN(ING)?\b/.test(t)) return 'text-amber-600 dark:text-amber-400';
		return '';
	}
	const logLines = $derived(logText ? logText.split('\n') : []);
</script>

<svelte:head>
	<title>{id} — rask</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<div class="flex flex-col gap-4 p-6 text-sm">
		<a
			href="{base}/jobs"
			class="text-muted-foreground hover:text-foreground inline-flex w-fit items-center gap-1 text-xs"
		>
			<ArrowLeft class="h-3.5 w-3.5" /> all jobs
		</a>

		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 text-destructive p-3">{error}</Card>
		{/if}

		{#if loaded && !job}
			<Card class="border-amber-500/40 bg-amber-500/10 p-4">
				No job with submission id <span class="font-mono">{id}</span> (it may have aged out of Ray's history).
			</Card>
		{/if}

		{#if job}
			<!-- Header -->
			<Card
				class="overflow-hidden border-l-2 {job.status === 'FAILED'
					? 'border-l-destructive'
					: job.status === 'SUCCEEDED'
						? 'border-l-emerald-500'
						: 'border-l-amber-500'}"
			>
				<div class="flex flex-wrap items-center gap-3 px-4 py-3">
					<Badge variant={jobVariant(job.status)} class={running ? 'animate-pulse' : ''}
						>{job.status}</Badge
					>
					<span class="font-mono font-semibold">{job.submission_id}</span>
					<span class="text-muted-foreground ml-auto text-xs">
						{#if job.start_time}started {fmtAgo(job.start_time)} ·{/if}
						runtime {fmtDur(
							job.start_time ? ((job.end_time ?? Date.now()) - job.start_time) / 1000 : null,
						)}
					</span>
				</div>

				<!-- WHICH DECLARATION produced this run. `rask.transform` is stamped by the medallion's submit
				     path into Ray's own `metadata`, which is readable from OUTSIDE the job and AFTER it
				     fails — the read this page makes. Before it existed the page could name the stage but
				     not the entrypoint and params the run was actually executing, so a person watching a
				     job had no path to the record that governs it.

				     ABSENT is a real state, not a gap to paper over: a mover with no MEDALLION_LANE runs
				     the chart's settings and there IS no record to link to. The row is omitted rather than
				     rendering a transform named nothing. -->
				{#if transformName}
					<div class="flex flex-wrap items-center gap-2 px-4 pb-3 text-xs">
						<span class="text-muted-foreground">transform</span>
						<a
							href="/compute/transforms"
							class="text-foreground hover:underline font-mono font-medium"
							data-slot="job-transform">{transformName}</a
						>
						{#if stageName}
							<span class="text-muted-foreground">· stage {stageName}</span>
						{/if}
					</div>
				{/if}

				{#if job.error_type || job.message}
					<!-- Ray sends `message` on EVERY terminal job, including "Job finished successfully." —
					     it is a status line, not an error. Keying the destructive styling on the message's
					     presence painted a red alert with a warning triangle directly under a green
					     SUCCEEDED badge, so the page told an operator two opposite things at once. The
					     failure signal is `error_type`, or a FAILED/STOPPED status. -->
					<div
						class={[
							'mx-4 mb-3 flex items-start gap-1.5 rounded-md border p-2 font-mono text-[11px]',
							jobFailed
								? 'border-destructive/30 bg-destructive/5 text-destructive'
								: 'border-border/60 bg-muted/40 text-muted-foreground',
						]}
					>
						{#if jobFailed}
							<TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" />
						{:else}
							<Info class="mt-0.5 h-3.5 w-3.5 shrink-0" />
						{/if}
						<span class="break-words"
							>{#if job.error_type}<span class="font-semibold"
									>{job.error_type}:
								</span>{/if}{job.message}</span
						>
					</div>
				{/if}

				<div
					class="grid grid-cols-2 gap-x-6 gap-y-1.5 border-t px-4 py-3 font-mono text-[11px] sm:grid-cols-3 lg:grid-cols-6"
				>
					{#each [{ k: 'job_id', v: job.job_id }, { k: 'started', v: fmtTime(job.start_time) }, { k: 'ended', v: fmtTime(job.end_time) }, { k: 'driver_exit', v: job.driver_exit_code ?? '—' }] as row (row.k)}
						<div class="flex justify-between gap-2 border-b border-dashed py-0.5">
							<span class="text-muted-foreground">{row.k}</span><span
								class="truncate"
								title={String(row.v ?? '')}>{row.v ?? '—'}</span
							>
						</div>
					{/each}
				</div>

				{#if job.entrypoint}
					<details class="group border-t">
						<summary
							class="text-muted-foreground hover:bg-muted/40 flex cursor-pointer list-none items-center gap-1 px-4 py-1.5 text-[11px]"
						>
							<ChevronRight class="h-3 w-3 transition-transform group-open:rotate-90" /> entrypoint
						</summary>
						<pre
							class="bg-muted/50 mx-4 mb-3 overflow-auto rounded-md p-2 font-mono text-[10px] whitespace-pre-wrap">{job.entrypoint}</pre>
					</details>
				{/if}
			</Card>

			<!-- Driver logs -->
			<Card class="overflow-hidden">
				<div class="flex items-center gap-2 border-b px-4 py-2">
					<span class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase"
						>Driver logs</span
					>
					{#if running}
						<span
							class="flex items-center gap-1 text-[11px] text-emerald-600 dark:text-emerald-400"
						>
							<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500"></span>live
						</span>
					{/if}
					<button
						class="hover:bg-muted ml-auto inline-flex h-6 items-center gap-1 rounded-md border px-2 text-[11px] disabled:opacity-50"
						onclick={refreshLogs}
						disabled={logsLoading}
					>
						<RefreshCw class="h-3 w-3 {logsLoading ? 'animate-spin' : ''}" /> refresh
					</button>
				</div>
				<div
					bind:this={logPre}
					onscroll={onLogScroll}
					class="max-h-[40vh] overflow-auto py-1 font-mono text-[11px] leading-relaxed"
				>
					{#each logLines as l, i (i)}
						<div class="hover:bg-muted/40 px-3 whitespace-pre-wrap {logLineClass(l)}">
							{l || ' '}
						</div>
					{/each}
					{#if !logLines.length}
						<div class="text-muted-foreground p-3">
							{logsLoading ? 'loading…' : '(no driver logs)'}
						</div>
					{/if}
				</div>
			</Card>

			<!-- Tasks (runner-pipeline jobs) -->
			{#if jobTasks.length}
				<Card class="overflow-hidden">
					<div
						class="text-muted-foreground border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
					>
						Tasks ({jobTasks.length})
					</div>
					<div class="max-h-[50vh] overflow-auto">
						<table class="w-full border-collapse text-xs">
							<thead class="bg-card sticky top-0 z-10 text-left">
								<tr class="border-b">
									<SortHeader label="state" col="state" {sortKey} {sortDir} onsort={setSort} />
									<SortHeader label="type" col="type" {sortKey} {sortDir} onsort={setSort} />
									<SortHeader label="task" col="task" {sortKey} {sortDir} onsort={setSort} />
									<SortHeader label="node" col="node" {sortKey} {sortDir} onsort={setSort} />
									<SortHeader label="pid" col="pid" {sortKey} {sortDir} onsort={setSort} />
									<SortHeader label="started" col="started" {sortKey} {sortDir} onsort={setSort} />
									<SortHeader
										label="duration"
										col="duration"
										{sortKey}
										{sortDir}
										onsort={setSort}
									/>
								</tr>
							</thead>
							<tbody>
								{#each sortedTasks as t (t.task_id)}
									<tr class="border-border/40 hover:bg-muted/40 border-b">
										<td class="px-3 py-1.5"
											><Badge
												variant={taskStateVariant(t.state)}
												class={t.state === 'RUNNING' ? 'animate-pulse' : ''}>{t.state}</Badge
											></td
										>
										<td class="text-muted-foreground px-3 py-1.5 font-mono">{typeLabel(t.type)}</td>
										<td class="px-3 py-1.5">
											<div class="font-mono">{t.func_or_class_name ?? t.name ?? '—'}</div>
											{#if t.error_message}
												<div
													class="text-destructive flex items-start gap-0.5 font-mono text-[10px]"
													title={t.error_message}
												>
													<TriangleAlert class="mt-0.5 h-3 w-3 shrink-0" />{t.error_type ? `${t.error_type}: ` : ''}{t.error_message}
												</div>
											{/if}
										</td>
										<td class="px-3 py-1.5 font-mono">{nodeLabel(t.node_id)}</td>
										<td class="px-3 py-1.5 font-mono tabular-nums">{t.worker_pid ?? '—'}</td>
										<td class="text-muted-foreground px-3 py-1.5 font-mono tabular-nums"
											>{fmtAgo(taskStart(t))}</td
										>
										<td class="px-3 py-1.5 font-mono tabular-nums">{fmtDur(taskDuration(t))}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				</Card>
			{:else}
				<div class="text-muted-foreground flex items-center gap-2 text-xs">
					<FileText class="h-4 w-4" />
					No Ray tasks for this job — htr_http jobs run as a plain HTTP driver; the work happens in the
					Serve replicas (Actors page).
				</div>
			{/if}
		{/if}
	</div>
</main>
