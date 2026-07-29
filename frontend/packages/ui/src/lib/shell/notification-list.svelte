<script lang="ts">
	import { CircleCheck, CircleDashed, CircleX, LoaderCircle, X } from '@lucide/svelte';
	import { Badge } from '../components/badge/index.js';
	import { Button } from '../components/button/index.js';
	import { Progress } from '../components/progress/index.js';
	import { cn } from '../utils/cn.js';
	import { formatAbsolute, formatRelative } from '../utils/timestamp.js';
	import {
		runJobLabel,
		runNotificationId,
		runPhase,
		runPhaseLabel,
		runProgress,
		visibleRuns,
		type RunStatusLike,
	} from '../runs/run-status.js';

	// The rows of the notification surface: one per run, newest first, rendering the lifecycle the
	// lineage service actually folds — STARTED / RUNNING (with progress when the producer stamped a
	// total) / COMPLETED / FAILED with its error text. Split out of `notification-center` so the
	// panel's MARKUP can be rendered and asserted on its own: the bell's popover content is
	// portalled, and a portal renders nothing on the server, so a test that only mounted the centre
	// could never see a row. It is also the piece a zone would reuse for a full-page feed.
	//
	// Pure presentation: it takes runs, it never fetches, and it never mutates — dismissing is
	// reported UP via `ondismiss` so the owner of the dismissed set stays the owner.
	let {
		runs,
		seen = [],
		dismissed = [],
		limit = 8,
		now = Date.now(),
		ondismiss,
		class: className,
	}: {
		/** Runs as `GET /runs` returns them (any order — this sorts). */
		runs: RunStatusLike[];
		/** Notification ids the viewer has already read; anything else is marked unread. */
		seen?: string[];
		/** Notification ids the viewer dismissed — filtered out here too, so a caller that forgets to
		 *  pre-filter still cannot show a dismissed row. */
		dismissed?: string[];
		/** How many rows the panel shows before it says how many more there are. */
		limit?: number;
		/** Injectable clock, so a test can pin "5m ago" (same seam `formatTimestamp` already has). */
		now?: number;
		ondismiss?: (notificationId: string) => void;
		class?: string;
	} = $props();

	const ordered = $derived(visibleRuns(runs, dismissed));
	const shown = $derived(ordered.slice(0, limit));
	const readSet = $derived(new Set(seen));

	const icon = (run: RunStatusLike) => {
		switch (runPhase(run)) {
			case 'failed':
				return CircleX;
			case 'complete':
				return CircleCheck;
			case 'running':
			case 'started':
				return LoaderCircle;
			default:
				return CircleDashed;
		}
	};

	// The pill colour carries the same meaning as the icon — success/destructive/warning are the
	// estate's tokens (tokens.css), not per-component hexes.
	const tone = (run: RunStatusLike): 'success' | 'destructive' | 'warning' | 'secondary' => {
		switch (runPhase(run)) {
			case 'failed':
				return 'destructive';
			case 'complete':
				return 'success';
			case 'running':
			case 'started':
				return 'warning';
			default:
				return 'secondary';
		}
	};

	const iconTone = (run: RunStatusLike) => {
		switch (runPhase(run)) {
			case 'failed':
				return 'text-destructive';
			case 'complete':
				return 'text-success';
			case 'running':
			case 'started':
				return 'text-warning';
			default:
				return 'text-muted-foreground';
		}
	};
</script>

{#if shown.length === 0}
	<p class="text-muted-foreground px-3 py-6 text-center text-sm">
		No run activity yet. Runs appear here as the lineage service records them.
	</p>
{:else}
	<ul class={cn('divide-border/60 divide-y', className)} data-slot="notification-list">
		{#each shown as run (run.run_id)}
			{@const id = runNotificationId(run)}
			{@const job = runJobLabel(run)}
			{@const progress = runProgress(run)}
			{@const phase = runPhase(run)}
			{@const Icon = icon(run)}
			<li
				class="flex items-start gap-2.5 px-3 py-2.5"
				data-slot="notification"
				data-phase={phase}
				data-unread={readSet.has(id) ? undefined : ''}
			>
				<Icon
					class={cn('mt-0.5 size-4 shrink-0', iconTone(run), phase === 'running' && 'animate-spin')}
					aria-hidden="true"
				/>
				<div class="min-w-0 flex-1">
					<div class="flex min-w-0 items-center gap-2">
						<!-- `leading-none` sets a 14px line box for a 14px font, and `truncate` brings `overflow:hidden`
						     with it, so every descender is sliced off: measured clientH 14 vs scrollH 16, which rendered
						     "ingest_events" as "inqest_events" and "aggregate_gold" as "aqqreqate_qold" in
						     docs/audits/shots/notifications-panel-open.png. It survived a row-by-row reading of that
						     screenshot because a reader silently corrects the words — it took a 4x zoom to see. -->
						<p class="truncate text-sm leading-tight font-medium" title={run.job ?? run.run_id}>
							{job.name}
						</p>
						<Badge variant={tone(run)} class="shrink-0">{runPhaseLabel(run)}</Badge>
						{#if !readSet.has(id)}
							<!-- The unread mark rides the row, not just the count: opening the panel must not be
							     the same thing as having read every row in it. -->
							<span class="bg-primary ml-auto size-1.5 shrink-0 rounded-full" aria-label="Unread"
							></span>
						{/if}
					</div>
					<p class="text-muted-foreground mt-1 truncate text-xs">
						<!-- Joined in the script, not stitched from `{#if}` blocks in the markup: the compiler
						     trims the trailing space inside a block, and the first cut of this line rendered
						     "lance-medallion ·alice". -->
						{[job.namespace, run.author ?? 'unknown author'].filter(Boolean).join(' · ')} ·
						<span title={formatAbsolute(run.updated_at ?? run.started_at)}>
							{formatRelative(run.updated_at ?? run.started_at, now)}
						</span>
					</p>
					{#if progress}
						<!-- Only when the producer stamped a total. A run with no progress facet gets no bar,
						     because a 0% bar on a run that is simply un-instrumented is a lie. -->
						<div class="mt-1.5 flex items-center gap-2">
							<Progress value={progress.percent} class="h-1.5" />
							<span class="text-muted-foreground shrink-0 text-xs tabular-nums">
								{progress.done} of {progress.total}
							</span>
						</div>
					{/if}
					{#if run.error_message}
						<!-- Clamped to three lines, with the whole thing on hover. Unclamped, a real failure from
						     the estate rendered a 25-line Rust stack trace — object paths, byte offsets, rustc
						     source lines — that filled the panel and pushed every other notification out of
						     view. A notification says what broke; a log says why, and this is not the log.
						     Caught by opening the screenshot: every assertion passed, because the text WAS
						     present — far too much of it. -->
						<p
							class="text-destructive mt-1.5 line-clamp-3 text-xs break-words"
							data-slot="notification-error"
							title={run.error_message}
						>
							{run.error_message}
						</p>
					{/if}
					{#if (run.outputs ?? []).length > 0}
						<p class="text-muted-foreground mt-1 truncate text-xs">
							→ {(run.outputs ?? []).join(', ')}
						</p>
					{/if}
				</div>
				<Button
					variant="ghost"
					size="icon-xs"
					class="shrink-0"
					aria-label={`Dismiss ${job.name}`}
					onclick={() => ondismiss?.(id)}
				>
					<X aria-hidden="true" />
				</Button>
			</li>
		{/each}
	</ul>
	{#if ordered.length > shown.length}
		<p class="text-muted-foreground border-border/60 border-t px-3 py-2 text-center text-xs">
			{ordered.length - shown.length} older {ordered.length - shown.length === 1 ? 'run' : 'runs'} not shown
		</p>
	{/if}
{/if}
