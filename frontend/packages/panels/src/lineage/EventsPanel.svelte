<script lang="ts">
	/**
	 * The OpenLineage event feed, with a live staleness readout.
	 *
	 * Two of the drag test's four cases live here. The scrolling list carries SCROLL OFFSET, and the
	 * "Ns ago" footer is a genuine running counter — a `setInterval` started in `onMount` and cleared
	 * on destroy. If the dock remounted this panel on a layout change the interval would be torn down
	 * and restarted, and the age would visibly snap back to 0s. It ticking straight through a drag is
	 * the most legible proof that the panel was re-parented rather than rebuilt.
	 *
	 * It is also honest UI rather than a test fixture: a feed that says "live" while showing minutes-old
	 * events is the failure this readout makes visible.
	 */
	import { onMount } from 'svelte';
	import { getLineageState } from './lineage-context.js';

	const store = getLineageState();

	/** Seconds this panel has been mounted. Ticks across every layout change; never resets. */
	let mountedFor = $state(0);

	onMount(() => {
		// POLL REASON: this advances a CLOCK, not a feed — it issues no request and subscribes to
		// nothing. The estate's lineage cursor already gates every data read, and a cursor cannot help
		// here because the value that changes is elapsed time itself: there is no event meaning "one
		// more second passed", and the readout's entire job is to keep moving while nothing happens.
		// Structurally the same survivor the gate's own docstring blesses — a moving-window value whose
		// staleness is the signal. One timer, one panel, torn down on destroy.
		const timer = setInterval(() => (mountedFor += 1), 1000);
		return () => clearInterval(timer);
	});

	const events = $derived([...store.events].reverse());
</script>

<div class="panel">
	<div class="scroll">
		{#if events.length === 0}
			<p class="empty">No events in the window.</p>
		{:else}
			<ul>
				<!-- Keyed on `seq`, the feed's own monotonic id. An index key would re-use DOM across a
				     poll that prepends events, so every row's content would shift under the user. -->
				{#each events as ev (ev.seq)}
					<li>
						<span class="kind" class:fail={/FAIL|ABORT/i.test(ev.event_type ?? '')}>
							{ev.event_type ?? '—'}
						</span>
						<span class="job" title={ev.job ?? ''}>{ev.job ?? '—'}</span>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
	<footer>
		<span class="dot" class:on={store.online}></span>
		{store.online ? 'live' : 'offline'}
		<span class="sep">·</span>
		panel open <span class="mono">{mountedFor}s</span>
		{#if store.lastUpdated}<span class="sep">·</span> last read {store.lastUpdated}{/if}
	</footer>
</div>

<style>
	.panel {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
	}
	.scroll {
		flex: 1 1 0;
		min-height: 0;
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
		gap: 8px;
		padding: 5px 12px;
		border-bottom: 1px solid var(--border);
		font-size: 12px;
	}
	.kind {
		flex: 0 0 auto;
		font-weight: 600;
		color: var(--muted-foreground);
	}
	.kind.fail {
		color: var(--destructive);
	}
	.job {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	footer {
		display: flex;
		align-items: center;
		gap: 6px;
		flex: 0 0 auto;
		padding: 5px 12px;
		border-top: 1px solid var(--border);
		font-size: 11px;
		color: var(--muted-foreground);
	}
	.dot {
		width: 7px;
		height: 7px;
		border-radius: 999px;
		background: var(--muted-foreground);
	}
	.dot.on {
		background: var(--success);
	}
	.sep {
		opacity: 0.5;
	}
	.mono {
		font-variant-numeric: tabular-nums;
	}
</style>
