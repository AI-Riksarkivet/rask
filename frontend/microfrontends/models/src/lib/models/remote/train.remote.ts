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

/** Submit a training run. Validated to the door's OWN shape, so a refusal is caught before the POST. */
export const submitTraining = command(
	TrainRequestSchema,
	async (body): Promise<ApiResult<TrainAccepted>> =>
		parsedGateway(
			await gatewayJSON('/api/train', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(body),
			}),
			TrainAcceptedSchema,
		),
);
