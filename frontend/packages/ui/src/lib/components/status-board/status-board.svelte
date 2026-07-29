<script lang="ts">
	/** Live run-status board: one row per run with a state pill, animated progress bar, and error
	 * strip. Transport-agnostic (rask convention: the caller polls and passes `runs`; the lib never
	 * owns an API client) — the run shape is `RunStatusLike` from `$lib/runs`, the package's ONE
	 * reading of the lineage service's `RunStatus`. It used to be re-declared here, a second copy
	 * missing `started_at`, `events` and `operation`; the notification surface reads the same rows,
	 * so the type is now shared rather than forked. */
	import { CheckCircle2, LoaderCircle, XCircle, CircleDashed } from '@lucide/svelte';
	import { enter, bar, breathe } from '../../motion';
	import type { RunStatusLike } from '../../runs/run-status.js';

	let { runs }: { runs: RunStatusLike[] } = $props();

	const isRunning = (s?: string | null) => /START|RUNNING/i.test(s ?? '');
	const isFail = (s?: string | null) => /FAIL|ABORT/i.test(s ?? '');
	const stateIcon = (s?: string | null) =>
		isFail(s)
			? XCircle
			: s === 'COMPLETE'
				? CheckCircle2
				: isRunning(s)
					? LoaderCircle
					: CircleDashed;
	const color = (s?: string | null) =>
		isFail(s)
			? 'var(--fail)'
			: s === 'COMPLETE'
				? 'var(--ok)'
				: isRunning(s)
					? 'var(--amber)'
					: 'var(--mut)';

	const shortJob = (j?: string | null) => (j ?? '').replace(/^ray-jobs\//, '');

	function pct(r: RunStatusLike): number {
		if (r.state === 'COMPLETE') return 100;
		if (r.progress_total) return Math.round(((r.progress_done ?? 0) / r.progress_total) * 100);
		return isRunning(r.state) ? 8 : 0;
	}
	function label(r: RunStatusLike): string {
		if (r.state === 'COMPLETE') return 'COMPLETE';
		if (isFail(r.state)) return r.state ?? 'FAIL';
		if (isRunning(r.state)) return `RUNNING ${r.progress_done ?? 0}/${r.progress_total ?? 3}`;
		return r.state ?? '—';
	}
	const time = (s?: string | null) => (s ? new Date(s).toLocaleTimeString() : '');
</script>

<div class="board">
	{#if runs.length === 0}
		<p class="hint">
			No runs yet — trigger a step and watch them go <b>queued → running → done/failed</b>.
		</p>
	{/if}
	{#each runs as r (r.run_id)}
		{@const StateIcon = stateIcon(r.state)}
		<div
			class="row"
			class:fail={isFail(r.state)}
			class:running={isRunning(r.state)}
			style:--c={color(r.state)}
			{@attach enter({ y: 8 })}
		>
			<div class="top">
				<span class="job mono" title={r.job}>{shortJob(r.job)}</span>
				<span class="pill">
					<StateIcon size={12} class={isRunning(r.state) ? 'spin' : ''} />
					{label(r)}
				</span>
			</div>
			<div class="track">
				<div class="fill" {@attach bar(pct(r))} {@attach breathe(isRunning(r.state))}></div>
			</div>
			<div class="meta">
				<span class="who"
					>{r.author ?? '—'}{#if (r.outputs ?? []).length}
						· → {(r.outputs ?? []).join(', ')}{/if}</span
				>
				<span class="who">{time(r.updated_at)}</span>
			</div>
			{#if r.error_message}<div class="err">{r.error_message}</div>{/if}
		</div>
	{/each}
</div>

<style>
	.board {
		display: flex;
		flex-direction: column;
		gap: 9px;
	}
	.hint {
		color: var(--mut);
		font-size: 12px;
		line-height: 1.5;
	}
	.hint b {
		color: var(--ink);
	}
	.row {
		position: relative;
		border: 1px solid var(--line);
		border-left: 3px solid var(--c);
		border-radius: var(--radius-sm);
		padding: 9px 11px;
		background: linear-gradient(180deg, var(--panel-2), var(--panel));
		transition: border-color 0.3s var(--ease);
	}
	.row.running {
		border-color: color-mix(in srgb, var(--amber) 35%, var(--line));
	}
	.top {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 8px;
	}
	.job {
		font-size: 12px;
		color: var(--ink);
		font-weight: 600;
	}
	.pill {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.3px;
		padding: 2px 8px;
		border-radius: 999px;
		color: var(--c);
		border: 1px solid color-mix(in srgb, var(--c) 50%, transparent);
		background: color-mix(in srgb, var(--c) 12%, transparent);
		white-space: nowrap;
	}
	.pill :global(.spin) {
		animation: spin 1s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	.track {
		height: 6px;
		background: #0b0f17;
		border-radius: 999px;
		overflow: hidden;
		margin: 8px 0 6px;
		box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
	}
	.fill {
		height: 100%;
		width: 0;
		border-radius: 999px;
		background: linear-gradient(90deg, color-mix(in srgb, var(--c) 70%, #000), var(--c));
	}
	.meta {
		display: flex;
		justify-content: space-between;
		gap: 8px;
	}
	.who {
		font-size: 11px;
		color: var(--mut);
	}
	.err {
		color: var(--fail);
		font-size: 11px;
		margin-top: 5px;
		padding-top: 5px;
		border-top: 1px dashed color-mix(in srgb, var(--fail) 40%, transparent);
	}
</style>
