/**
 * Shared state for voice search ("find this voice" — query-by-example
 * navigation, see api.ts).
 *
 * Three concerns live here so they can be shared without prop drilling
 * (mirrors the `features` flags singleton):
 *
 *  - `status` / `built`  one `/api/voice/status` probe gates every voice
 *    affordance (hit-card buttons, the Settings toggle). `probe()` is
 *    idempotent — the search page calls it on mount.
 *
 *  - `pending` + `request()`  THE entry point for starting a voice search
 *    from anywhere: hit cards, the diarization timeline, URL deep-links.
 *    Requests queue in `pending`; the search page (+page.svelte) consumes
 *    them in an `$effect` and runs `/api/voice/similar`. Timeline builders:
 *    call `voiceSearch.request({ docId, turnId | speaker | t }, label?)` —
 *    no props or context needed. (Deep-links work too: `/?voice_doc=…&
 *    voice_turn=…|voice_speaker=…|voice_t=…`.) `requestUpload(file)` is the
 *    same lifecycle for an uploaded clip (the search-bar attach button).
 *
 *  - `includeSameDoc`  the Settings "include same video" toggle (default
 *    OFF → `exclude_same_doc=true` server-side). The search page re-runs an
 *    active voice query immediately when it flips.
 */

import { getVoiceStatus, type VoiceAnchor, type VoiceStatus } from '@rask/explorer-api';

export type VoiceRequest =
	| {
			anchor: VoiceAnchor;
			/** Chip label for the anchor (e.g. the video filename stem). Falls back to
			 *  the response's `doc_id` when omitted (timeline / deep-link callers). */
			label?: string | undefined;
			/** One-shot retry anchor used when `anchor` 404s. ASR chunk spans and
			 *  diarization turns disagree at boundaries, so a text hit's midpoint
			 *  `t`-anchor can land in a mid-speech diarization gap — hit-card passes
			 *  the chunk start here as a second try before an error surfaces. */
			fallback?: VoiceAnchor | undefined;
	  }
	| {
			/** Query-by-uploaded-clip: rank turns against the clip's own voiceprint
			 *  (POST `/api/voice/similar`) instead of a Lance anchor row. */
			upload: File;
			label: string;
	  };

class VoiceSearchStore {
	/** `null` until the one-shot status probe resolves. */
	status = $state<VoiceStatus | null>(null);
	/** Latest unconsumed voice-search request (the search page consumes it). */
	pending = $state<VoiceRequest | null>(null);
	/** Settings toggle: also surface hits from the anchor's own video. */
	includeSameDoc = $state(false);
	/** In-flight uploaded-clip fetches; a counter (not a boolean) so a rapid
	 *  second pick can't clear the spinner while its successor is still
	 *  running. The first upload after a backend restart lazy-loads the voice
	 *  encoder (~5 s), so the attach button must visibly spin. */
	#uploadsInFlight = $state(0);

	#probed = false;

	/** True once the backend reports the voice tables are built. */
	get built(): boolean {
		return this.status?.built ?? false;
	}

	/** Fetch `/api/voice/status` once; later calls are no-ops. A failed probe
	 *  reads as "not built" (all voice UI stays hidden) — never throws. */
	probe(): void {
		if (this.#probed) return;
		this.#probed = true;
		getVoiceStatus()
			.then((s) => {
				this.status = s;
			})
			.catch(() => {
				this.status = { built: false, turns: 0, speakers: 0 };
			});
	}

	/** True while an uploaded-clip search is in flight (attach-button spinner). */
	get uploadPending(): boolean {
		return this.#uploadsInFlight > 0;
	}

	/** Start a voice search for `anchor` (auto-applies — no submit step). */
	request(anchor: VoiceAnchor, label?: string, fallback?: VoiceAnchor): void {
		this.pending = { anchor, label, fallback };
	}

	/** Start a voice search ranked against an uploaded audio/video clip
	 *  (auto-applies, same pending/chip lifecycle as `request`).
	 *  `exclude_same_doc` doesn't apply — the clip isn't a Lance row. */
	requestUpload(file: File): void {
		this.pending = { upload: file, label: `uploaded clip: ${file.name}` };
	}

	/** Bracket one uploaded-clip fetch (the search page wraps its
	 *  `voiceSimilarUpload` call) so `uploadPending` tracks it. */
	beginUpload(): void {
		this.#uploadsInFlight += 1;
	}

	endUpload(): void {
		this.#uploadsInFlight = Math.max(0, this.#uploadsInFlight - 1);
	}
}

// Module-scope rune state under ssr=true: safe ONLY while every mutation stays browser-gated
// (onMount/$effect/handlers) — the full invariant + the two-session SSR proof live at the
// `graph` singleton in workflow/graph.svelte.ts (#97).
export const voiceSearch = new VoiceSearchStore();
