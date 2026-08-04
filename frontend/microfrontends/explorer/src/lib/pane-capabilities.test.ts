/**
 * Which panes a corpus may show.
 *
 * The reported defect: viewing a still IMAGE showed a time scrubber, a Transcript tab and a Speakers
 * tab. The media ELEMENT was already chosen from the declared mime — an image corpus correctly got an
 * `<img>` — but the chrome around it sat inside `{#if !isFullscreen}` and nothing else, so every
 * corpus got the whole audio/video apparatus whether or not a word of it applied.
 *
 * The gates use the capability names the SERVICE reads (`transcripts.py` looks up `alignments`,
 * `diarization.py` looks up `diarization`), so a declaration cannot mean one thing to the UI and
 * another to the endpoint behind it.
 */

import { describe, expect, it } from 'vitest';

import { panesFor, shownTab, type ViewerCapabilities } from './pane-capabilities';

const view = (isPlayable: boolean, capabilities: string[] = []): ViewerCapabilities => ({
	isPlayable,
	hasCapability: (name) => capabilities.includes(name),
});

const IMAGE_CORPUS = view(false);
const AV_CORPUS = view(true, ['alignments', 'diarization']);

describe('what an IMAGE corpus offers', () => {
	it('offers no timeline, no transcript and no speakers', () => {
		// The reported bug, stated directly.
		expect(panesFor(IMAGE_CORPUS)).toEqual({
			timeline: false,
			transcript: false,
			speakers: false,
			tabs: false,
		});
	});

	it('shows NO sync body at all rather than an empty one', () => {
		// An empty panel reads as a failed load; absence reads as "not applicable".
		expect(shownTab('transcript', panesFor(IMAGE_CORPUS))).toBeNull();
	});
});

describe('the gates are independent', () => {
	it('a time axis alone does not conjure a transcript', () => {
		// A video corpus with no alignment table: scrubbing is meaningful, karaoke is not.
		const panes = panesFor(view(true));

		expect(panes.timeline).toBe(true);
		expect(panes.transcript).toBe(false);
		expect(panes.speakers).toBe(false);
		expect(panes.tabs).toBe(false);
	});

	it('alignments without diarization offers Transcript only', () => {
		const panes = panesFor(view(true, ['alignments']));

		expect([panes.transcript, panes.speakers]).toEqual([true, false]);
		expect(panes.tabs).toBe(true);
	});

	it('diarization without alignments offers Speakers only', () => {
		const panes = panesFor(view(true, ['diarization']));

		expect([panes.transcript, panes.speakers]).toEqual([false, true]);
		expect(panes.tabs).toBe(true);
	});

	it('a capability for something ELSE opens nothing', () => {
		// `frames` is what the seeded corpus actually declares.
		expect(panesFor(view(true, ['frames']))).toEqual({
			timeline: true,
			transcript: false,
			speakers: false,
			tabs: false,
		});
	});

	it('a fully-declared corpus offers everything', () => {
		expect(panesFor(AV_CORPUS)).toEqual({
			timeline: true,
			transcript: true,
			speakers: true,
			tabs: true,
		});
	});
});

describe('before a descriptor exists', () => {
	it('offers nothing rather than everything', () => {
		expect(panesFor(null)).toEqual({
			timeline: false,
			transcript: false,
			speakers: false,
			tabs: false,
		});
	});
});

describe('which tab is actually shown', () => {
	it('honours the pick when the corpus supports it', () => {
		expect(shownTab('speakers', panesFor(AV_CORPUS))).toBe('speakers');
		expect(shownTab('transcript', panesFor(AV_CORPUS))).toBe('transcript');
	});

	it('falls back when the pick is unavailable HERE', () => {
		// Someone on Speakers switches to a corpus with only alignments. The pick is untouched — this
		// is derived, not a corrected `tab` — so switching back restores it.
		expect(shownTab('speakers', panesFor(view(true, ['alignments'])))).toBe('transcript');
		expect(shownTab('transcript', panesFor(view(true, ['diarization'])))).toBe('speakers');
	});
});
