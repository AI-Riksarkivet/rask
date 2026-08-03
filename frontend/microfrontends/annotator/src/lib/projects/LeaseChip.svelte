<script lang="ts">
	// A2's honest lease chip. The wall clock is part of the truth here: a lease that lapsed between
	// server reminder ticks must read EXPIRED, never "held" — showing a corpse lease as held is how
	// an annotator loses work to a reclaim they were never warned about.
	import { Badge } from '@rask/ui/badge';

	import { leaseView, formatLease } from './lease.js';
	import type { TaskDetail } from './types.js';

	let {
		task,
		me,
	}: { task: Pick<TaskDetail, 'state' | 'assignee' | 'lease_expires_at'>; me: string | null } =
		$props();

	let now = $state(new Date());
	$effect(() => {
		// POLL REASON: a lease deadline is a wall-clock fact with no event — the countdown (and the
		// held→expired flip) can only be observed by sampling time. 1 s matches the chip's own
		// resolution; the SERVER remains the authority (its reminder fires lease_expired regardless).
		const timer = setInterval(() => (now = new Date()), 1000);
		return () => clearInterval(timer);
	});

	const view = $derived(leaseView(task, me, now));
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
