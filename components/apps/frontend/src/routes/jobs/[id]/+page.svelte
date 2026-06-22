<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/state';
	import {
		rayJobs,
		rayJobLogs,
		tasksList,
		rayCluster,
		listBatches,
		type RayJob,
		type TaskInfo,
		type RayNode,
		type BatchRow,
	} from '$lib/api';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Card } from '$lib/components/ui/card';
	import { Badge, type BadgeVariant } from '@rask/ui/badge';
	import { SortHeader } from '@rask/ui/sort-header';
	import { ArrowLeft, TriangleAlert, FileText, ChevronRight, RefreshCw } from 'lucide-svelte';

	const id = $derived(decodeURIComponent(page.params.id ?? ''));

	let jobs = $state<RayJob[]>([]);
	let tasks = $state<TaskInfo[]>([]);
	let nodes = $state<RayNode[]>([]);
	let batches = $state<BatchRow[]>([]);
	let logText = $state('');
	let logsLoading = $state(false);
	let loaded = $state(false);
	let error = $state<string | null>(null);
	let timer: ReturnType<typeof setInterval> | null = null;
	let tickCount = 0;
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
	const running = $derived(job?.status === 'RUNNING' || job?.status === 'PENDING');

	async function refreshLogs() {
		if (!id) return;
		logsLoading = true;
		try {
			const p = await rayJobLogs(id);
			if (p.ok) logText = p.logs;
		} catch {
			/* job may have aged out */
		} finally {
			logsLoading = false;
		}
	}

	async function refresh() {
		try {
			const [j, t] = await Promise.all([rayJobs(), tasksList()]);
			jobs = j.jobs ?? [];
			tasks = t;
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loaded = true;
		}
		// Heavier fetches on a slower cadence: batches (1MB+) every 3rd tick,
		// logs every tick while the job is live (else only on demand).
		if (tickCount % 3 === 0) {
			try {
				batches = (await listBatches()).batches;
			} catch {
				/* progress card is a nicety */
			}
		}
		if (running || tickCount === 0) await refreshLogs();
		try {
			nodes = (await rayCluster()).nodes ?? [];
		} catch {
			/* node names optional */
		}
		tickCount += 1;
	}

	onMount(() => {
		refresh();
		timer = setInterval(refresh, 5000);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	// Tail behaviour for the log pane.
	function onLogScroll() {
		if (logPre) logStick = logPre.scrollHeight - logPre.scrollTop - logPre.clientHeight < 40;
	}
	$effect(() => {
		logText;
		if (logPre && logStick) logPre.scrollTop = logPre.scrollHeight;
	});

	const nodeMap = $derived(new Map(nodes.map((n) => [n.node_id, n])));
	const jobTasks = $derived(job?.job_id ? tasks.filter((t) => t.job_id === job.job_id) : []);

	// Per-batch progress for this job (chunk jobs carry their batch ids).
	const jobBatches = $derived.by<BatchRow[]>(() => {
		if (!job?.batches.length || !batches.length) return [];
		const want = new Set(job.batches);
		return batches.filter((b) => want.has(b.batch_id));
	});
	const progress = $derived.by(() => {
		const total = jobBatches.reduce((a, b) => a + (b.page_count ?? 0), 0);
		const done = jobBatches.reduce((a, b) => a + (b.transcribed_pages ?? 0), 0);
		return { total, done, pct: total ? (done / total) * 100 : 0 };
	});

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
	function batchVariant(s: string | null): 'success' | 'secondary' | 'warning' {
		if (s === 'done') return 'success';
		if (s === 'partial') return 'warning';
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
	<title>{id} — RASK</title>
</svelte:head>

<RayShell title="Job">
	<div class="flex flex-col gap-4 p-6 text-sm">
		<a
			href="/jobs"
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

				{#if job.error_type || job.message}
					<div
						class="border-destructive/30 bg-destructive/5 text-destructive mx-4 mb-3 flex items-start gap-1.5 rounded-md border p-2 font-mono text-[11px]"
					>
						<TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" />
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
					{#each [{ k: 'job_id', v: job.job_id }, { k: 'started', v: fmtTime(job.start_time) }, { k: 'ended', v: fmtTime(job.end_time) }, { k: 'batches', v: job.batches.length }, { k: 'chunk', v: job.metadata?.chunk_id != null ? `${job.metadata.chunk_id}/${job.metadata.chunk_total ?? '?'}` : '—' }, { k: 'driver_exit', v: job.driver_exit_code ?? '—' }] as row (row.k)}
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

			<!-- Batch progress -->
			{#if jobBatches.length}
				<Card class="overflow-hidden">
					<div class="flex items-center gap-3 border-b px-4 py-2">
						<span class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase"
							>Progress</span
						>
						<div class="bg-muted h-1.5 w-44 overflow-hidden rounded-full">
							<div
								class="h-full bg-emerald-500 transition-all"
								style:width={`${progress.pct}%`}
							></div>
						</div>
						<span class="font-mono text-xs tabular-nums"
							>{progress.done.toLocaleString()}/{progress.total.toLocaleString()} pages ({progress.pct.toFixed(
								1,
							)}%)</span
						>
					</div>
					<div class="grid gap-x-6 px-4 py-2 sm:grid-cols-2 lg:grid-cols-3">
						{#each jobBatches as b (b.batch_id)}
							{@const pct = b.page_count ? ((b.transcribed_pages ?? 0) / b.page_count) * 100 : 0}
							<div class="flex items-center gap-2 py-1 text-xs">
								<span class="w-20 shrink-0 font-mono">{b.batch_id}</span>
								<Badge variant={batchVariant(b.htr_status)} class="scale-90">{b.htr_status}</Badge>
								<div class="bg-muted h-1.5 min-w-0 flex-1 overflow-hidden rounded-full">
									<div class="h-full bg-emerald-500 transition-all" style:width={`${pct}%`}></div>
								</div>
								<span
									class="text-muted-foreground w-24 shrink-0 text-right font-mono text-[10px] tabular-nums"
								>
									{b.transcribed_pages ?? 0}/{b.page_count ?? 0}
								</span>
							</div>
						{/each}
					</div>
				</Card>
			{/if}

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
													<TriangleAlert class="mt-0.5 h-3 w-3 shrink-0" />{t.error_type
														? `${t.error_type}: `
														: ''}{t.error_message}
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
</RayShell>
