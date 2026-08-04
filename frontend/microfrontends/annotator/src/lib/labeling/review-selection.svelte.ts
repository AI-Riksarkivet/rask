/**
 * The read→annotate bridge: a selection of media units to review, stepped through in
 * the annotator. The READ plane (atlas lasso / search) forms it — deep-linking to
 * `/annotator?keys=k1,k2,…` where each key is the descriptor key-path
 * (`doc/speech/chunk`) — and the annotator route opens it here + navigates. This is
 * the `Selection` bridge made concrete (see labeling/types.ts).
 */
import { base } from '$app/paths';

import type { MediaKind, MediaUnit } from '$lib/viewer/types';

/** Build an annotator unit from a key-path (`doc/speech/chunk`) — the same path the
 *  chunk-frame + annotations endpoints take, under THIS zone's base (the same-origin
 *  BFF proxy routes at `/annotator/api/*`). Modality defaults to image/document (our
 *  corpus); `kind` selects the temporal viewers (audio waveform / video frame-overlay),
 *  whose media source defaults to the doc's `/api/explorer` stream — `mediaUrl` overrides
 *  it (a deep-link can annotate any same-origin media, e.g. a fixture clip). A
 *  non-default `dataset` rides every URL as `?dataset=` (the media services' selector;
 *  omitted ⇒ the backend default dataset), so frame/annotations/save all target the
 *  dataset the unit was picked from — never silently the default one. */
function unitFromKey(
	key: string,
	kind: MediaKind = 'image',
	mediaUrl?: string,
	dataset?: string | null,
): MediaUnit {
	const doc = key.split('/')[0];
	const ds = dataset ? `?dataset=${encodeURIComponent(dataset)}` : '';
	return {
		kind,
		key,
		imageUrl: `${base}/api/chunk-frame/${key}${ds}`,
		...(kind === 'audio' || kind === 'video'
			? { mediaUrl: mediaUrl ?? `${base}/api/explorer/${doc}${ds}` }
			: {}),
		annotationsUrl: `${base}/api/annotations/${key}${ds}`,
	};
}

class ReviewSelection {
	units = $state<MediaUnit[]>([]);
	index = $state(0);
	/** Dataset the open units belong to — null for the backend default (no `?dataset=`). */
	dataset = $state<string | null>(null);
	/** The annotation TASK this canvas was opened from (`?task=` on the queue's Annotate link) —
	 *  null for ad-hoc viewing. When set, a save also snapshots the unit's shapes into the task's
	 *  draft, so the work travels into the publish. */
	taskId = $state<string | null>(null);

	get active(): MediaUnit | null {
		return this.units[this.index] ?? null;
	}
	get total(): number {
		return this.units.length;
	}

	/** Open a selection of key-paths for review (from the read plane). `kind`/`mediaUrl`/
	 *  `dataset` apply to every unit (the deep-link's modality, media-source and
	 *  dataset-selector overrides). */
	openKeys(
		keys: string[],
		kind: MediaKind = 'image',
		mediaUrl?: string,
		dataset?: string | null,
		taskId?: string | null,
	): void {
		this.dataset = dataset ?? null;
		this.taskId = taskId ?? null;
		this.units = keys.filter(Boolean).map((k) => unitFromKey(k, kind, mediaUrl, dataset));
		this.index = 0;
	}
	go(i: number): void {
		if (i >= 0 && i < this.units.length) this.index = i;
	}
	clear(): void {
		this.units = [];
		this.index = 0;
		this.dataset = null;
		this.taskId = null;
	}
}

/** Singleton — written by the read plane (via the route's `?keys=`), read by the
 *  annotator route + its PageNav. */
export const reviewSelection = new ReviewSelection();
