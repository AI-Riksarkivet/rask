/**
 * Moving through a queue one item at a time WITHOUT leaving it.
 *
 * A pre-labelled queue is reviewed, not drawn on: look at the image, check the suggested class is
 * right, accept or send it back. Today that means clicking "Annotate →", landing on the canvas
 * route, deciding, navigating back — and losing the filter, the page and the selection on the way.
 * Fifty-seven items is fifty-seven round trips through a page you did not want to leave.
 *
 * Label Studio calls this Quick View. This is the navigation half: where you are in the current
 * page, and what prev/next mean at the ends.
 *
 * Scoped to a REVIEW pass on purpose. It shows the item and its verdict actions; it is not the
 * PixiJS drawing surface, and it does not pretend to be — an item that needs shapes drawn still
 * belongs on the canvas.
 */

import type { TaskDetail } from './types';

/** Where the open item sits, and what is either side of it. */
export interface Neighbours {
	/** 1-based, for display ("3 of 20"). Zero when the item is not in the list at all. */
	position: number;
	total: number;
	prev: TaskDetail | null;
	next: TaskDetail | null;
}

/**
 * Locate a task among the rows currently in view.
 *
 * The list passed in is the TABLE's current page, so prev/next walk exactly what the filter and
 * sort produced — reviewing in the order you are looking at things, rather than in the order the
 * server happened to return them.
 *
 * Deliberately does NOT wrap. Reaching the end of a page and silently jumping to the start makes a
 * review pass loop forever with no signal that you have seen everything; a disabled Next says you
 * are done here.
 */
export function neighbours(tasks: readonly TaskDetail[], taskId: string | null): Neighbours {
	const index = taskId === null ? -1 : tasks.findIndex((t) => t.task_id === taskId);
	if (index === -1) return { position: 0, total: tasks.length, prev: null, next: null };
	return {
		position: index + 1,
		total: tasks.length,
		prev: tasks[index - 1] ?? null,
		next: tasks[index + 1] ?? null,
	};
}

/**
 * Which task the drawer should show after an action closed one out.
 *
 * Accepting an item is a verdict, and the point of a review pass is the NEXT one — so it advances
 * rather than closing. At the end of the page it returns `null`, which the caller reads as "close":
 * pretending there is more to review would be a lie about the queue's state.
 */
export function afterVerdict(tasks: readonly TaskDetail[], taskId: string): string | null {
	return neighbours(tasks, taskId).next?.task_id ?? null;
}
