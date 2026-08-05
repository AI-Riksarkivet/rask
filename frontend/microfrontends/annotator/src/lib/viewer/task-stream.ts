/**
 * The LABEL STREAM — the set of items the canvas can walk while you are labeling.
 *
 * Reported: "in anno i can't even naviagte between the claimed labling items???"
 *
 * Correct, and the shape of the miss is worth naming: the canvas HAD navigation, and it was
 * `PageNav` — prev/next across the `keys` of ONE task. So you could page through the pages of a
 * document and could not reach the next item you had claimed. Finishing an item meant going back to
 * the queue and clicking the next Annotate button, every time. Every labeling tool the product is
 * measured against (Label Studio, CVAT) makes the queue itself the navigation set, because that is
 * the loop the work actually has — the unit of work is the ITEM, not the page.
 *
 * The two navigations coexist: `PageNav` moves WITHIN an item, this moves BETWEEN items. They are
 * different axes and neither replaces the other.
 *
 * WHOSE items. The stream is the tasks CLAIMED BY YOU, not every task in the project. A stream over
 * the whole queue would step you into items another annotator holds a lease on — the canvas would
 * open, you would draw, and the save would be refused by the server (or worse, accepted for someone
 * else's lease). Claimed-by-me is the set you can actually act on, which makes "next" always a legal
 * move rather than sometimes one.
 */

/** The fields of a task this module needs. Structural on purpose: the queue's `TaskDetail` fits, and
 *  so does a test fixture, without either importing the other's schema. */
export interface StreamTask {
	task_id: string;
	state: string;
	assignee?: string | null;
	source: { keys?: string[]; where?: string | null };
	/** Optional, and only the filmstrip reads it: the declared media kind picks the fallback glyph
	 *  when an item has no frame. Optional so a fixture that only exercises ORDERING does not have to
	 *  carry a media block it never looks at. */
	media?: { kind?: string } | null;
}

/**
 * The tasks you can walk, in the order the queue lists them.
 *
 * ORDER IS THE CALLER'S, deliberately: this filters, it does not sort. The queue is already sorted —
 * by the column you chose, with the filters you set — and a stream that re-sorted would mean "next"
 * on the canvas and "next row" in the table are different items. Sorting here would be a second
 * opinion about order, and there is no way for the person to see which one won.
 */
export function claimedStream(
	tasks: StreamTask[],
	me: string | null,
	authEnabled = true,
): StreamTask[] {
	const claimed = tasks.filter((t) => t.state === 'claimed');

	// AUTH OFF — there is one anonymous principal, so every claimed item IS yours.
	//
	// This distinction is load-bearing and the first cut got it wrong: gating purely on `me` meant the
	// stream was permanently empty on an ungoverned stack, because `capi/v1/me` 404s when auth is
	// disabled. The feature would have been invisible on exactly the stack it was being demonstrated
	// on — indistinguishable from not having been built. "No identity" is not one condition; it is
	// two, and only one of them is dangerous.
	if (!authEnabled) return claimed;

	// AUTH ON, identity unresolved → empty. THIS is the dangerous case: other principals exist, so
	// guessing hands you an item someone else holds a lease on — the canvas opens, you draw, the save
	// is refused. An empty stream degrades to the old no-cross-item behaviour, which is honest.
	if (!me) return [];

	return claimed.filter((t) => t.assignee === me);
}

/** Where the open task sits in the stream, or -1 when it is not in it at all. */
export function streamIndex(stream: StreamTask[], taskId: string | null): number {
	if (!taskId) return -1;
	return stream.findIndex((t) => t.task_id === taskId);
}

/**
 * The neighbour in a direction, or null at the end.
 *
 * It does NOT wrap. Wrapping hides the end of the queue: you press next, land on the item you
 * started from, and cannot tell "you have been round" from "nothing moved". A disabled control at
 * the boundary says you are done with what you claimed, which is information worth having.
 *
 * A task NOT in the stream (index -1) has no neighbours rather than defaulting to the first item —
 * the canvas can be opened on an item you do not hold (from the corpus browser, or after a lease
 * lapsed), and silently teleporting to someone else's first claimed task would be the worst possible
 * answer to pressing an arrow.
 */
export function neighbour(
	stream: StreamTask[],
	taskId: string | null,
	step: 1 | -1,
): StreamTask | null {
	const at = streamIndex(stream, taskId);
	if (at < 0) return null;
	return stream[at + step] ?? null;
}

/**
 * The canvas URL for a task in the stream.
 *
 * Byte-for-byte the shape `TaskQueue.canvasHref` builds, and for the same reason: `project` rides
 * beside `task` so the canvas's EXIT can address the queue page. A stream hop that dropped it would
 * re-break the exit two items in — the bug fixed one commit ago, reintroduced by the feature that
 * makes hopping possible.
 */
export function taskCanvasHref(task: StreamTask, projectId: string, base = ''): string {
	const keys = (task.source.keys ?? []).join(',');
	const dataset = task.source.where ? `dataset=${encodeURIComponent(task.source.where)}&` : '';
	return `${base}/?${dataset}keys=${encodeURIComponent(keys)}&task=${encodeURIComponent(
		task.task_id,
	)}&project=${encodeURIComponent(projectId)}`;
}

/** What the canvas needs to render the stream control: position, bounds, and where the arrows go. */
export interface StreamPosition {
	/** 1-based for display; 0 when the open item is not in the stream. */
	position: number;
	total: number;
	prevHref: string | null;
	nextHref: string | null;
	/** True when the canvas is walking a queue at all — false hides the control entirely. */
	active: boolean;
	/** The stream itself, so the FILMSTRIP renders the very list the arrows step through.
	 *
	 *  Carried here rather than computed again beside the strip: two computations of "your claimed
	 *  items, in queue order" would eventually disagree about what "next" is, and the person would
	 *  have two answers on one screen with no way to tell which was right. One call, both surfaces. */
	items: StreamTask[];
	/** Which item is open, and which queue it belongs to — the strip needs both to mark the active
	 *  tile and to build each tile's href. */
	taskId: string | null;
	projectId: string | null;
}

export function streamPosition(
	stream: StreamTask[],
	taskId: string | null,
	projectId: string | null,
	base = '',
): StreamPosition {
	const at = streamIndex(stream, taskId);
	// No project → the canvas was not opened from a queue, so there is no stream to show. Rendering
	// "0 / 0" there would advertise a feature that does not apply to this canvas.
	const active = projectId !== null && at >= 0;
	const prev = active ? neighbour(stream, taskId, -1) : null;
	const next = active ? neighbour(stream, taskId, 1) : null;
	return {
		position: at + 1,
		total: stream.length,
		prevHref: prev && projectId ? taskCanvasHref(prev, projectId, base) : null,
		nextHref: next && projectId ? taskCanvasHref(next, projectId, base) : null,
		active,
		// The strip renders whenever there IS a queue, even standing on an item outside it (a lapsed
		// lease, or a canvas opened from the browser while you hold other items). `active` gates the
		// ARROWS, which cannot step from a position that does not exist; the strip has no such
		// problem and hiding it would remove the only way back to your queue.
		items: projectId ? stream : [],
		taskId,
		projectId,
	};
}
