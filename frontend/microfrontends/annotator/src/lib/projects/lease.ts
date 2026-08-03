/**
 * Honest lease presentation — the A2 contract, as pure functions.
 *
 * "A claimed task whose lease died is not the annotator's any more, and showing it as theirs
 * invites lost work" (OPEN-WORK.md § Design — annotation projects, A2). The server is the authority — `lease_expired` fires from
 * an actor reminder — but between reminder ticks the CLIENT must not display a corpse lease as
 * held. These helpers derive the honest presentation from the task document and the current time;
 * they decide nothing (the backend refuses stale writes regardless).
 */
import type { TaskDetail } from './types.js';

export type LeaseView =
	| { kind: 'unheld' }
	/** Manager-assigned: `lease_expires_at` is null while claimed — it never expires. */
	| { kind: 'pinned'; assignee: string }
	| { kind: 'held'; assignee: string; secondsLeft: number; mine: boolean }
	/** The wall clock passed `lease_expires_at` but the server has not yet fired `lease_expired` —
	 *  shown as expired, NEVER as held: the claim is gone in every way that matters. */
	| { kind: 'expired'; assignee: string };

export function leaseView(
	task: Pick<TaskDetail, 'state' | 'assignee' | 'lease_expires_at'>,
	me: string | null,
	now: Date = new Date(),
): LeaseView {
	if (task.state !== 'claimed' || !task.assignee) return { kind: 'unheld' };
	if (!task.lease_expires_at) return { kind: 'pinned', assignee: task.assignee };
	const msLeft = new Date(task.lease_expires_at).getTime() - now.getTime();
	if (msLeft <= 0) return { kind: 'expired', assignee: task.assignee };
	return {
		kind: 'held',
		assignee: task.assignee,
		secondsLeft: Math.floor(msLeft / 1000),
		mine: me !== null && task.assignee === me,
	};
}

/** `mm:ss` under an hour, `h:mm:ss` above — the countdown chip's text. */
export function formatLease(secondsLeft: number): string {
	const s = Math.max(0, secondsLeft);
	const hours = Math.floor(s / 3600);
	const minutes = Math.floor((s % 3600) / 60);
	const seconds = s % 60;
	const mmss = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
	return hours > 0 ? `${hours}:${mmss}` : mmss;
}
