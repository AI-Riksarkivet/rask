/**
 * Where the canvas's exit button goes.
 *
 * It went to `?dataset=…` — the corpus browser — always, and its own comment claimed "a
 * task-opened canvas exits back to wherever the annotator came from". It did not. Open an item from
 * your labeling queue, annotate it, press exit, and you land in a document gallery with your queue
 * nowhere in sight; the only way back was the browser's own Back button or navigating from scratch.
 *
 * The canvas already KNOWS where it came from — `TaskQueue` builds its link with `&task=<id>`, and
 * now `&project=<id>` beside it. Throwing that away on the way out was the bug.
 */

/** The URL the exit control should navigate to, relative to the zone base. */
export function exitHref(params: URLSearchParams, base = ''): string {
	// Came from a labeling task → go back to ITS queue, which is the list the person is working
	// through. `project` is what identifies the page; `task` alone cannot address it.
	const project = params.get('project');
	if (project) return `${base}/tasks/${encodeURIComponent(project)}`;

	// Otherwise this was the corpus browser's canvas hand-off, and the dataset is the context worth
	// keeping — landing on the default corpus after annotating a different one is a silent switch.
	const dataset = params.get('dataset');
	return dataset ? `${base}/?dataset=${encodeURIComponent(dataset)}` : `${base}/`;
}
