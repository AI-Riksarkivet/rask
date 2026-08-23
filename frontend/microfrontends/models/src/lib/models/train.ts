/**
 * The `POST /api/train` contract, mirrored — shapes only, so `train.remote.ts` can export nothing
 * but remote functions.
 *
 * THE PATTERNS ARE THE DOOR'S OWN. `MODEL_PATTERN`, `DATASET_PATTERN` and `MAX_FEATURES` are copied
 * from `medallion/services/train.py` deliberately rather than loosened: that door refuses a
 * malformed request 422 because "a request the consumer would DROP is refused HERE, never 202'd into
 * a silent no-op". Validating to the same shape in the browser means a person sees which field is
 * wrong while they are still looking at it, instead of a 422 after they press submit.
 *
 * NOTHING HERE NAMES A WORKLOAD, and that is the point of the rewrite. The page this replaces
 * offered dropdowns of one modality's models and corpora — an invented vocabulary the API never had.
 * The real contract is a model NAME, feature dataset refs, and an opaque config; a platform that
 * governs any modality cannot ship a form that only fits one.
 */

import * as v from 'valibot';

/** `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` — medallion/services/train.py::MODEL_PATTERN. */
export const MODEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
/** `stage$name`, each segment the model shape — medallion/services/train.py::DATASET_PATTERN. */
export const DATASET_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\$[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
/** medallion/services/train.py::MAX_FEATURES. */
export const MAX_FEATURES = 16;

/** One training input: a `stage$name` feature dataset, optionally pinned to an exact version. */
export const FeatureRefSchema = v.object({
	dataset: v.pipe(v.string(), v.regex(DATASET_PATTERN)),
	version: v.optional(v.nullable(v.number())),
});
export type FeatureRef = v.InferOutput<typeof FeatureRefSchema>;

/** The request body. `config` is opaque by design — the runner declares its own shape. */
export const TrainRequestSchema = v.object({
	model: v.pipe(v.string(), v.regex(MODEL_PATTERN)),
	features: v.pipe(v.array(FeatureRefSchema), v.minLength(1), v.maxLength(MAX_FEATURES)),
	config: v.optional(v.record(v.string(), v.unknown())),
});
export type TrainRequest = v.InferOutput<typeof TrainRequestSchema>;

/** The 202 body. Fields are optional because the door's own response model is `None` — it answers a
 *  bare 202 today, and a future body must not turn a working submit into a parse failure. */
export const TrainAcceptedSchema = v.object({
	run_id: v.optional(v.nullable(v.string())),
	status: v.optional(v.nullable(v.string())),
});
export type TrainAccepted = v.InferOutput<typeof TrainAcceptedSchema>;

/** Parse the `stage$name[@version]` lines a person types into feature refs.
 *
 * Returns the refs AND the lines it could not read, rather than throwing on the first bad one: a
 * typo in line four should not discard lines one to three while someone is mid-edit.
 */
export function parseFeatureLines(text: string): { features: FeatureRef[]; invalid: string[] } {
	const features: FeatureRef[] = [];
	const invalid: string[] = [];
	for (const raw of text.split('\n')) {
		const line = raw.trim();
		if (!line) continue;
		const [name, pin] = line.split('@');
		const dataset = (name ?? '').trim();
		if (!DATASET_PATTERN.test(dataset)) {
			invalid.push(line);
			continue;
		}
		if (pin === undefined) {
			features.push({ dataset });
			continue;
		}
		const version = Number(pin.trim());
		if (!Number.isInteger(version) || version < 1) {
			invalid.push(line);
			continue;
		}
		features.push({ dataset, version });
	}
	return { features, invalid };
}
