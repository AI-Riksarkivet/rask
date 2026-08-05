/**
 * The queue's two view modes, and what a task looks like as a CARD.
 *
 * A list is how you audit a queue; a grid is how you scan one. For a corpus of page images the grid
 * is the mode that answers "which of these are letters" in one glance, and it is the mode Label
 * Studio's Data Manager opens in for image data. rask had list only — so the way to judge forty
 * items was to read forty rows of keys.
 *
 * Both modes share ONE selection (the same `rowSelection`, keyed by task id), so the bulk actions
 * work identically from either. That is the property that makes a grid worth having rather than a
 * second, weaker table.
 */

import type { TaskDetail } from './types';

/** How the queue is being read right now. */
export type QueueView = 'list' | 'grid';

export const QUEUE_VIEW_KEY = 'rask-annotator-queue-view';

/** Parse a stored view mode. Anything unrecognised is `list` — the mode that works for every corpus,
 *  including the ones with no imagery to put in a grid. */
export function parseView(raw: string | null): QueueView {
	return raw === 'grid' ? 'grid' : 'list';
}

/**
 * The image to show for a task, or `null` when there is nothing to show.
 *
 * Two sources, in order:
 *
 * 1. `media.image_url`, when a sender supplied one. No sender does today — the explorer's dialog and
 *    the annotator's bulk bar both send `media: {kind}` alone — but the field is on the wire and
 *    honouring it costs one line.
 * 2. The viewer's frame endpoint, built from the item's KEY. This is the same construction
 *    `DatasetView.frameUrl` uses (`/api/chunk-frame/<key path>` plus the dataset query), because a
 *    URL assembled differently here would 404 against the very cache the browse grid populates.
 *
 * `null` rather than a placeholder path when there is no key: a broken <img> is worse than an empty
 * tile, because it looks like the item failed rather than like it has no preview.
 */
export function taskImageUrl(task: TaskDetail, apiUrl: (path: string) => string): string | null {
	if (task.media.image_url) return task.media.image_url;

	// The FIRST key. An item is one thing a person looks at and decides about, so it normally has
	// exactly one key; a multi-key item is a scope, and showing its first frame is a better answer
	// than showing nothing.
	const key = task.source.keys[0];
	if (!key) return null;

	const dataset = task.source.where;
	const query = dataset ? `?dataset=${encodeURIComponent(dataset)}` : '';
	return apiUrl(`/api/chunk-frame/${key}${query}`);
}

/**
 * A one-line caption for a card — what the tile says about the item beyond its picture.
 *
 * The key alone is unreadable at card size (`fe00cd746463ad2c/1/12`), so this leads with the part
 * that varies within a document and keeps the whole key as the tooltip. Deliberately NOT the label:
 * the label is rendered as its own badge, because a suggested class must be visually distinct from
 * the item's identity.
 */
export function cardCaption(task: TaskDetail): string {
	const key = task.source.keys[0] ?? task.task_id;
	const parts = key.split('/');
	return parts.length > 1 ? parts.slice(1).join('/') : key;
}
