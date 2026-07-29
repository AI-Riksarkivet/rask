<script lang="ts">
	/**
	 * Ray job submissions.
	 *
	 * Reads through the zone-provided `ComputeReads` context — see `./context.ts` for why a remote
	 * function cannot live in a package (SvelteKit hashes its endpoint id per app). Read imperatively through `.current` rather than `await`, because a
	 * panel is mounted AFTER hydration and never renders on the server; a top-level await here would
	 * suspend a subtree the server never produced.
	 */
	import { Badge } from '@rask/ui/badge';
	import { getComputeReads } from './context.js';

	const jobs = getComputeReads().getRayJobs();
	const rows = $derived(jobs.current?.jobs ?? []);

	function tone(status: string): 'success' | 'destructive' | 'secondary' {
		if (/SUCCEEDED/i.test(status)) return 'success';
		if (/FAILED|STOPPED/i.test(status)) return 'destructive';
		return 'secondary';
	}
</script>

<div class="scroll">
	{#if rows.length === 0}
		<p class="empty">No job submissions.</p>
	{:else}
		<ul>
			{#each rows as job (job.submission_id)}
				<li>
					<Badge variant={tone(job.status)}>{job.status}</Badge>
					<span class="id" title={job.entrypoint ?? ''}>{job.submission_id}</span>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.scroll {
		height: 100%;
		overflow-y: auto;
	}
	.empty {
		margin: 0;
		padding: 16px;
		font-size: 12px;
		color: var(--muted-foreground);
	}
	ul {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	li {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 12px;
		border-bottom: 1px solid var(--border);
		font-size: 12px;
	}
	.id {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}
</style>
