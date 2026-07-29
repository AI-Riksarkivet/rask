/**
 * The viewer/annotation ABSTRACTION — the seam that keeps modalities decoupled.
 *
 * The only axis that forks per media kind is the VIEWER component (image = PixiJS
 * canvas, audio = waveform, video = frame-overlay + timeline). Everything else —
 * the annotation model, the store, the annotator service — stays uniform. Adding a
 * modality = one registry entry + one component that satisfies `ViewerProps`, not a
 * rewire. (See RA_ANNO_MERGE.md §5c–5d.)
 */

/** Media modality of a unit to annotate — the sole per-modality fork point. */
export type MediaKind = 'image' | 'audio' | 'video';

/** Everything a viewer needs to render + annotate one unit, resolved from the
 *  descriptor upstream so the viewer itself is decoupled from routing/data. */
export interface MediaUnit {
	kind: MediaKind;
	/** Stable id (doc + identity keys joined) — the annotation key scope. */
	key: string;
	/** Backdrop image URL: the page image (image) or the current frame (video). */
	imageUrl?: string;
	/** Time-media URL: the `<audio>`/`<video>` source (audio, video). */
	mediaUrl?: string;
	/** Arrow-IPC endpoint for this unit's annotations. */
	annotationsUrl: string;
	/** Natural media dimensions when known. */
	width?: number;
	height?: number;
}

/** The props EVERY viewer component accepts — the decoupling contract. A new
 *  modality's component takes exactly these, so the shell/route never special-cases. */
export interface ViewerProps {
	unit: MediaUnit;
	/** Fired once the viewer has loaded + rendered (`count` = annotations shown). */
	onload?: (count: number) => void;
	/** Fired when the unit's media or annotations fail to load — the shell surfaces the
	 *  message honestly instead of hanging on "loading…" (403/404/network alike). */
	onerror?: (message: string) => void;
	/** Optional route-level annotator facade. A spatial viewer `attach`es its engine
	 *  + loaded table here so the ra-anno layout (toolbar/sidebar/zoom/layers) can bind
	 *  to a single reactive source; temporal viewers may attach their own surface later.
	 *  Omitted ⇒ the viewer renders read-only, exactly as before. */
	controller?: import('./annotator.svelte').AnnotatorController;
}

// NOTE: aspirational SpatialGeom/TemporalFacet/Annotation model types were removed
// 2026-07-21 (never referenced) — the shapes actually flowing through the system are
// the controller's AnnoRow/InsertRow over the mode-blind annotations table; the
// "one model spans all modalities" idea lives in the SCHEMA (t_start/t_end columns
// beside the spatial ones), not in a parallel TS type.
