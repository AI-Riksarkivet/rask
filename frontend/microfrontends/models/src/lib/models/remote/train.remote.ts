import { command } from '$app/server';
import type { ApiResult } from '@rask/api/client';
import { TrainAcceptedSchema, TrainRequestSchema, type TrainAccepted } from '../train';

import { gatewayJSON, parsedGateway } from '$lib/server/doors';
// Transport: the zone's GATEWAY door. `POST /api/train` is root-mounted on the medallion producer
// and reached through the gateway's `/api/train` row.
//
// A COMMAND, not a `+server.ts` route: the body is a model name, a handful of dataset refs and a
// small config — a typed VALUE on both sides, which is the estate's rule for remote functions.
// `/train` is explicitly a claim-check door ("pointers only"), so nothing large ever rides it.
//
// NO SINGLE-FLIGHT REFRESH HERE, deliberately. The sibling commands in this zone refresh the query
// they just changed; a training submission has no such query — the run it starts is detached, lands
// in LINEAGE minutes later, and the runs board reads that on the lineage cursor. Refreshing a
// registry that cannot have changed yet would be theatre.

/**
 * A stable key for one training submission.
 *
 * Content-derived rather than random: the point is that a RETRY converges. A `crypto.randomUUID()`
 * here would satisfy the door's 422 and reintroduce the exact defect the requirement exists to close,
 * because every replay would carry a fresh key.
 *
 * The door constrains the header to `^[A-Za-z0-9._-]{1,64}$`, so the JSON is hashed rather than
 * interpolated — a dataset name with a `$` in it (which is every governed table: `silver$features`)
 * would otherwise be refused 422 by the very validation this key is meant to satisfy.
 */
function trainingKey(body: unknown): string {
	const json = JSON.stringify(body);
	// FNV-1a, 32-bit. Not cryptographic and does not need to be: this is a dedupe key scoped to one
	// caller's retries, not a security boundary.
	let hash = 0x811c9dc5;
	for (let i = 0; i < json.length; i += 1) {
		hash ^= json.charCodeAt(i);
		hash = Math.imul(hash, 0x01000193) >>> 0;
	}
	return `ui-train-${hash.toString(36)}`;
}

/** Submit a training run. Validated to the door's OWN shape, so a refusal is caught before the POST. */
export const submitTraining = command(
	TrainRequestSchema,
	async (body): Promise<ApiResult<TrainAccepted>> =>
		parsedGateway(
			await gatewayJSON('/api/train', {
				method: 'POST',
				headers: {
					'content-type': 'application/json',
					// REQUIRED by the door, and deterministic on purpose. `/api/train` is a cascade head
					// behind a Dapr sidecar that replays 5xx, and the door used to mint a token per
					// attempt — so a retried 500 started a second unrelated training run. The key is now
					// mandatory (422 without), and deriving it from the request content means a retry of
					// the SAME submission converges while a genuinely different one does not. Same shape
					// the compute zone already uses for `/api/ingests`.
					'idempotency-key': trainingKey(body),
				},
				body: JSON.stringify(body),
			}),
			TrainAcceptedSchema,
		),
);
