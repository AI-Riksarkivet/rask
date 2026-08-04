/**
 * Shared one-element audio preview for result rows/cards — "listen to this
 * chunk" without loading it into the player pane.
 *
 * ONE HTMLAudioElement lives here (created lazily on the first play, i.e.
 * inside a user gesture), so starting any clip pauses whatever else was
 * playing — previews can never overlap, and the play/pause buttons all read
 * one source of truth (`playing`, keyed by utils.hitKey). The source is the
 * same range-streamed endpoint the player uses (api.mediaUrl) plus a media
 * fragment `#t=start,end`, so the browser range-requests straight to the
 * chunk; engines that ignore the END fragment are stopped by the timeupdate
 * guard below. Mirrors the `voiceSearch` singleton pattern.
 */
import { SvelteSet } from 'svelte/reactivity';
import { activeView } from '@rask/explorer-api/descriptor';

/** Grace past the clip end before the manual stop kicks in — engines that DO
 *  honor the end fragment pause themselves first and never reach it. */
const END_SLACK_S = 0.25;

class AudioPreviewStore {
	/** Row key (utils.hitKey) of the clip now playing; null = idle. */
	playing = $state<string | null>(null);
	/** doc_ids whose media failed to load (404 etc.) — their buttons disable. */
	#failedDocs = new SvelteSet<string>();

	#el: HTMLAudioElement | null = null;
	/** Clip end (s) for the timeupdate guard; Infinity = play to the doc end. */
	#end = Infinity;
	/** doc_id of the loaded source, so the error handler can attribute it. */
	#docId: string | null = null;

	isPlaying(key: string): boolean {
		return this.playing === key;
	}

	/** True when this doc's media previously failed to load — disable the button. */
	isFailed(docId: string): boolean {
		return this.#failedDocs.has(docId);
	}

	/** Play `[start, end]` of `docId`'s media, keyed by `key` (a row identity).
	 *  Calling with the key that is already playing pauses instead — every
	 *  button is a toggle. Starting a clip pauses any other row's clip. */
	toggle(clip: { key: string; docId: string; start: number; end: number }): void {
		const { key, docId, start, end } = clip;
		if (this.playing === key) {
			this.#el?.pause();
			return;
		}
		if (this.#failedDocs.has(docId)) return;
		const el = (this.#el ??= this.#create());
		el.pause();
		this.#docId = docId;
		this.#end = end > start ? end : Infinity;
		// Media fragment: seek to start and (where the engine honors it) stop at
		// end. Build the doc's media URL through the descriptor's doc key.
		const view = activeView();
		const src = view.mediaUrl({ [view.docKeyField]: docId });
		el.src = `${src}#t=${start}${end > start ? `,${end}` : ''}`;
		this.playing = key;
		void el.play().catch((err: unknown) => {
			// Quickly switching to another row aborts this play() — not a failure.
			if (err instanceof DOMException && err.name === 'AbortError') return;
			if (this.playing === key) this.playing = null;
			this.#failedDocs.add(docId);
		});
	}

	#create(): HTMLAudioElement {
		const el = new Audio();
		el.preload = 'none';
		// ANY pause — manual toggle, natural end, the timeupdate guard — clears
		// the playing row. `paused` flips false the moment a new play() starts,
		// so a late pause event from a superseded source can't clear the new row.
		el.addEventListener('pause', () => {
			if (el.paused) this.playing = null;
		});
		el.addEventListener('ended', () => {
			this.playing = null;
		});
		el.addEventListener('error', () => {
			// Fires only for the CURRENT source (a superseded load aborts silently).
			if (this.#docId) this.#failedDocs.add(this.#docId);
			this.playing = null;
		});
		el.addEventListener('timeupdate', () => {
			// Engines that ignore the end fragment would play through the clip.
			if (el.currentTime >= this.#end + END_SLACK_S) el.pause();
		});
		return el;
	}
}

export const audioPreview = new AudioPreviewStore();
