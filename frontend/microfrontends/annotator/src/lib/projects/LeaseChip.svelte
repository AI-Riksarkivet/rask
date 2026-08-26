<script lang="ts">
	// A2's honest lease chip. The wall clock is part of the truth here: a lease that lapsed between
	// server reminder ticks must read EXPIRED, never "held" — showing a corpse lease as held is how
	// an annotator loses work to a reclaim they were never warned about.
	import { Badge } from '@rask/ui/badge';

	import { wallClock } from './clock.svelte';
	import { leaseView, formatLease } from './lease.js';
	import type { TaskDetail } from './types.js';

	let {
		task,
		me,
	}: { task: Pick<TaskDetail, 'state' | 'assignee' | 'lease_expires_at'>; me: string | null } =
		$props();

	// ONE CLOCK FOR THE WHOLE QUEUE, not one per row. This component used to own a 1 s interval
	// itself, so a queue of forty tasks ran forty of them to compute the same instant — including for
	// the `pinned` and `unheld` rows, whose markup contains no countdown at all. `./clock.svelte`
	// holds the single interval and the POLL REASON (the wall clock is genuinely the only signal: no
	// event means "one more second passed").
	//
	// GUARDED OFF THE PROPS, deliberately not off `view`. `view` is derived FROM the clock, so
	// guarding on it would re-run this effect every tick and tear the subscription down and back up
	// once a second — the churn this change removes, reintroduced one layer up. A row that cannot
	// show a countdown subscribes to nothing, and a queue with no live leases runs no timer at all.
	$effect(() => {
		if (task.state !== 'claimed' || !task.lease_expires_at) return;
		return wallClock.subscribe();
	});

	const view = $derived(leaseView(task, me, wallClock.now));
</script>

{#if view.kind === 'held'}
	<Badge
		variant={view.mine ? 'default' : 'secondary'}
		title="lease expires in {formatLease(view.secondsLeft)}"
	>
		{view.mine ? 'yours' : view.assignee} · {formatLease(view.secondsLeft)}
	</Badge>
{:else if view.kind === 'pinned'}
	<Badge variant="secondary" title="assigned by a manager — the lease never expires">
		{view.assignee} · pinned
	</Badge>
{:else if view.kind === 'expired'}
	<Badge
		variant="destructive"
		title="the lease lapsed — this claim is gone; the task returns to the queue"
	>
		{view.assignee} · expired
	</Badge>
{/if}
