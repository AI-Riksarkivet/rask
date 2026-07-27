/**
 * The WRITE plane's producer catalog — the config-driven model registry (adopted
 * from X-AnyLabeling's YAML-per-model, as a typed registry, NOT a per-model
 * if/elif god-branch). Each producer declares WHAT it can do and WHERE it runs, so
 * the annotator surfaces ops generically and a new model is one registry entry.
 *
 * Read/write separation: NOTHING here reads or renders — that's the view+analyze
 * plane. These only PRODUCE annotation deltas from a Selection. `interactive`
 * producers run in the annotator (local-first → Save); `batch` producers run as
 * silver derivers (lance-ray) and surface async by media id + Lance version.
 */
import type { Execution, Op, ProducerKind } from './types';

export interface ProducerSpec {
	/** Registry key + provenance stamp written to `annotations.source`. */
	name: string;
	kind: ProducerKind;
	/** e.g. "human" | "model:grounding-dino" | "propagate:insid3" | "judge:vlm". */
	source: string;
	/** Ops this producer can perform. */
	ops: Op[];
	/** Where it can run. Corpus-scale producers are batch-only. */
	executions: Execution[];
	/** Few-shot producers need reference exemplars (INSID3 in-context masks). */
	needsExemplars: boolean;
	/** Richest primitive it emits (store the richest, project at review time). */
	outputs: 'mask' | 'bbox' | 'polygon' | 'text' | 'score' | 'field';
	/** The status a produced row lands in (human edits = accepted; models = prediction). */
	status: 'accepted' | 'rejected' | 'prediction' | 'reviewed';
	note: string;
}

export const PRODUCERS: Record<string, ProducerSpec> = {
	// ── Manual (human) — interactive, one|picked ──
	human: {
		name: 'human',
		kind: 'human',
		source: 'human',
		ops: ['set', 'verdict'],
		executions: ['interactive'],
		needsExemplars: false,
		outputs: 'field',
		status: 'accepted',
		note: 'Manual: set label/status on one annotation or a picked multi-selection (Label Studio).',
	},

	// ── AI-assisted — interactive (or quick batch), one|picked|all-from-small ──
	'sam-click': {
		name: 'sam-click',
		kind: 'model',
		source: 'model:sam',
		ops: ['predict'],
		executions: ['interactive'],
		needsExemplars: false,
		outputs: 'mask',
		status: 'prediction',
		note: 'The narrow interactive exception: encode-once-in-batch / decode-per-click point/box prompt.',
	},
	insid3: {
		name: 'insid3',
		kind: 'propagate',
		source: 'propagate:insid3',
		ops: ['propagate'],
		executions: ['interactive', 'batch'],
		needsExemplars: true,
		outputs: 'mask',
		status: 'prediction',
		note: "Training-free in-context segmentation on a frozen DINOv3 backbone (visinf/INSID3): 1–few exemplar masks → propagate to a selection/all. 'Apply to all from small data'.",
	},
	'grounding-dino': {
		name: 'grounding-dino',
		kind: 'model',
		source: 'model:grounding-dino',
		ops: ['predict'],
		executions: ['interactive', 'batch'],
		needsExemplars: false,
		outputs: 'bbox',
		status: 'prediction',
		note: 'Open-vocab detector: a text prompt → boxes over a selection/all. Interactive for a page, batch for a query.',
	},

	// ── Bulk / auto / judge — batch only, query|all (weak supervision + embeddings) ──
	htr: {
		name: 'htr',
		kind: 'model',
		source: 'model:htr',
		ops: ['predict'],
		executions: ['batch'],
		needsExemplars: false,
		outputs: 'text',
		status: 'prediction',
		note: 'HTR/OCR transcription as a silver deriver over bronze — whole-corpus batch, not interactive.',
	},
	'vlm-judge': {
		name: 'vlm-judge',
		kind: 'judge',
		source: 'judge:vlm',
		ops: ['judge'],
		executions: ['batch'],
		needsExemplars: false,
		outputs: 'score',
		status: 'reviewed',
		note: "AI-as-Judge: scores/verifies EXISTING predictions in batch (looks at data, not at a person's screen). Feeds confidence/uncertainty, never the interactive path.",
	},
	'embed-propagate': {
		name: 'embed-propagate',
		kind: 'propagate',
		source: 'propagate:embed',
		ops: ['propagate'],
		executions: ['batch'],
		needsExemplars: true,
		outputs: 'field',
		status: 'prediction',
		note: 'Weak-supervision label propagation over the embedding graph from a few labeled exemplars (FiftyOne-style bulk tagging on a query selection).',
	},
};

/** Producers that can run in the annotator (the write plane), for the op palette. */
export const interactiveProducers = (): ProducerSpec[] =>
	Object.values(PRODUCERS).filter((p) => p.executions.includes('interactive'));

/** Producers that run as batch derivers (queued from a query/all selection). */
export const batchProducers = (): ProducerSpec[] =>
	Object.values(PRODUCERS).filter((p) => p.executions.includes('batch'));
