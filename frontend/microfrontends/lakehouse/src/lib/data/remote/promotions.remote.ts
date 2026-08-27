import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import {
	DecisionSchema,
	HeldPromotionSchema,
	type Decision,
	type HeldPromotion,
} from '../promotions';

import { gatewayJSON, parsedGateway } from '$lib/server/doors';
// Transport: the zone's GATEWAY door, not the catalog one — the promotion endpoints are root-mounted
// on the medallion producer and reached through the gateway's `/api/promotions` row.
//
// WHY A REMOTE FUNCTION AND NOT A `+server.ts` ROUTE. The estate's transport ruling is one transport
// per payload KIND: typed values ride remote functions, bytes ride `+server.ts`. A decision is
// `{approved: boolean}` in and a small JSON ack out — a value on both sides.
//
// WHY THE BEARER MATTERS MORE HERE THAN USUAL. `decide_promotion` refuses a service token: the
// estate's shared credential cannot approve its own output, and the gateway's own daprd-stamped token
// buys a caller nothing on this route. The rung is `can_promote` — the VALIDATOR tier, deliberately
// above the ordinary publish's `can_update_tag` — checked against the promotion's own destination. A
// remote function runs on the zone server and forwards exactly one thing: the signed-in human's
// bearer. That is the only credential this door accepts.

const enc = encodeURIComponent;

/** The live review under `instanceId`, or a status a component can branch on.
 *
 * A 404 is the ORDINARY terminal state, not an error to hide: the producer answers
 * "no longer under review" once a decision has landed or the 72-hour timer has expired, and a
 * validator arriving at an already-answered link needs to be told that plainly rather than shown a
 * spinner or an empty card. */
export const getHeldPromotion = query(
	v.object({ instanceId: v.string() }),
	async ({ instanceId }): Promise<ApiResult<HeldPromotion>> =>
		parsedGateway(await gatewayJSON(`/api/promotions/${enc(instanceId)}`), HeldPromotionSchema),
);

/** Approve or reject a held promotion. Approving RESUMES the cascade.
 *
 *  REJECTING IS TERMINAL, and this said the opposite until 2026-08-27. `workflow.py:1062-1069`
 *  emits a `REJECTED` outcome and RETURNS — the orchestration completes, the instance 404s, and
 *  nothing can reopen it. Describing that as "leaves it held" invited a validator to reject a
 *  promotion believing they could revisit it, which is the one mistake this surface must not
 *  encourage: the decision is recorded against their subject and the cascade stops there.
 *
 * Single-flights its own read so the card re-renders from the server's answer rather than from an
 * assumption about what the decision did — `void`, never `await`, because the refresh must not gate
 * the mutation's own return (the estate's 73 commands all take this shape). */
export const decidePromotion = command(
	v.object({ instanceId: v.string(), approved: v.boolean() }),
	async ({ instanceId, approved }): Promise<ApiResult<Decision>> => {
		const result = parsedGateway(
			await gatewayJSON(`/api/promotions/${enc(instanceId)}/decision`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ approved }),
			}),
			DecisionSchema,
		);
		void getHeldPromotion({ instanceId }).refresh();
		return result;
	},
);
