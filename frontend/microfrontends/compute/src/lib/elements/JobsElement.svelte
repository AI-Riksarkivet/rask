<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-compute-jobs>` — the Ray jobs panel as a CUSTOM ELEMENT, built and served by the
	 * compute zone, mounted by the global workbench compositor (open_workbench.md).
	 *
	 * Three contracts this file must hold:
	 *  - Light DOM (`shadow: 'none'`): the host page's token cascade reaches in, and the element's
	 *    OWN styling is the scoped style block below over `var(--color-*)` tokens — never host-page
	 *    Tailwind utilities, which the host's build has no reason to have generated.
	 *  - Props are EXPLICIT (the CE bridge exposes only declared props as element properties) and
	 *    none start with `on` (the bridge would parse that as an event-listener attachment).
	 *  - Data goes out ONLY as a bubbling composed CustomEvent (`rask:select`); nothing in here may
	 *    import from another zone or from the compositor.
	 *
	 * The mount stamp + poll counter are the spike's no-remount proof: if a dock drag remounted the
	 * element, both would visibly reset.
	 */
	import { rayJobs, type RayJob } from '@rask/api';

	let { pollms = 5000 }: { pollms?: number } = $props();

	let jobs = $state<RayJob[]>([]);
	let error = $state<string | null>(null);
	let polls = $state(0);
	const mountedAt = new Date().toLocaleTimeString();

	$effect(() => {
		let live = true;
		const read = async () => {
			try {
				// A timeout-armed fetch, because a dead upstream HANGS rather than erroring (observed:
				// /api/ray/* with no Ray cluster deployed) — and a hung fetch would freeze the poll
				// counter at 0 with the empty state showing, which reads as "no jobs" instead of the
				// truth. Timing out turns it into the error state below.
				const payload = await rayJobs((url) => fetch(url, { signal: AbortSignal.timeout(4000) }));
				if (!live) return;
				jobs = payload.jobs ?? [];
				error = null;
			} catch (e) {
				if (live) error = e instanceof Error ? e.message : String(e);
			} finally {
				if (live) polls += 1;
			}
		};
		void read();
		// POLL REASON: job lifecycle — submissions move PENDING→RUNNING→SUCCEEDED/FAILED while the
		// panel is on screen, and the element has no server push channel across the zone boundary.
		const timer = setInterval(read, pollms);
		return () => {
			live = false;
			clearInterval(timer);
		};
	});

	function select(node: HTMLElement, job: RayJob) {
		node.dispatchEvent(
			new CustomEvent('rask:select', {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-compute-jobs',
					kind: 'ray-job',
					id: job.submission_id,
					label: job.entrypoint ?? job.submission_id,
				},
			}),
		);
	}
</script>

<div class="rask-ce-jobs">
	<p class="meta">
		mounted {mountedAt} · poll #{polls}
	</p>
	{#if error}
		<p class="error">Ray unreachable: {error}</p>
	{:else if jobs.length === 0}
		<p class="empty">No job submissions.</p>
	{:else}
		<table>
			<thead>
				<tr><th>Submission</th><th>Status</th><th>Entrypoint</th></tr>
			</thead>
			<tbody>
				{#each jobs as job (job.submission_id)}
					<tr onclick={(e) => select(e.currentTarget, job)}>
						<td class="mono">{job.submission_id.slice(0, 18)}</td>
						<td><span class="status" data-status={job.status}>{job.status}</span></td>
						<td class="mono dim">{job.entrypoint ?? '—'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>

<style>
	.rask-ce-jobs {
		display: block;
		height: 100%;
		overflow: auto;
		padding: 0.75rem;
		color: var(--color-foreground);
		font-size: 0.8125rem;
	}
	.meta {
		margin: 0 0 0.5rem;
		color: var(--color-muted-foreground);
		font-size: 0.6875rem;
	}
	.error {
		color: var(--color-destructive);
	}
	.empty {
		color: var(--color-muted-foreground);
	}
	table {
		width: 100%;
		border-collapse: collapse;
	}
	th {
		text-align: left;
		font-weight: 500;
		color: var(--color-muted-foreground);
		border-bottom: 1px solid var(--color-border);
		padding: 0.25rem 0.5rem;
	}
	td {
		border-bottom: 1px solid var(--color-border);
		padding: 0.375rem 0.5rem;
	}
	tbody tr {
		cursor: pointer;
	}
	tbody tr:hover {
		background: var(--color-muted);
	}
	.mono {
		font-family: var(--font-mono, monospace);
	}
	.dim {
		color: var(--color-muted-foreground);
	}
	.status {
		border: 1px solid var(--color-border);
		border-radius: 0.25rem;
		padding: 0 0.375rem;
		font-size: 0.6875rem;
	}
	.status[data-status='SUCCEEDED'] {
		color: var(--color-primary);
	}
	.status[data-status='FAILED'] {
		color: var(--color-destructive);
	}
</style>
