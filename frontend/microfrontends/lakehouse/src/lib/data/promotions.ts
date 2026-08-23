/**
 * A HELD PROMOTION — the shapes, kept out of `promotions.remote.ts` because a remote file may
 * export only remote functions.
 *
 * WHAT THIS IS. When a stage's quality gate finds a promotion unusual rather than broken, the mover
 * does not drop it: it parks a durable Dapr workflow on `wait_for_external_event('promotion_decision')`
 * raced against a 72-hour timer, and a validator answers it. Until this surface existed the answer
 * could only be given with `curl` — the estate could hold a promotion for three days and offer nobody
 * a way to release it.
 *
 * A BLOCK IS NOT A HOLD, and the difference is why `reasons` matters. A STRUCTURAL finding (a null
 * key, an unresolvable blob pointer) is a verdict: `resolve_review_policy` returns `block`, the
 * workflow completes immediately, and nobody is asked — because no approval makes corrupt data
 * right, and offering one would be a lie. Only a band breach — "this promotion is unusual" — reaches
 * a person. So every id this surface can act on is, by construction, a question rather than a fault.
 */

import * as v from 'valibot';

/** `PromotionSpec` as `GET /api/promotions/{id}` returns it while a review is live. */
export const HeldPromotionSchema = v.object({
	instance_id: v.string(),
	project: v.string(),
	from_dataset: v.string(),
	to_dataset: v.string(),
	/** WHY a person is being asked. `first_promotion` on a new dataset, `row_count_delta` on a band
	 *  breach — never a structural assertion, which blocks without asking. */
	reasons: v.array(v.string()),
	/** The timer the external event is racing. After it, the workflow decides without anyone. */
	approval_hours: v.optional(v.nullable(v.number())),
});
export type HeldPromotion = v.InferOutput<typeof HeldPromotionSchema>;

/** The 202 body of `POST /api/promotions/{id}/decision`. */
export const DecisionSchema = v.object({
	status: v.string(),
	instance_id: v.string(),
	approved: v.boolean(),
	/** The destination the decision acted on — echoed so the UI names what it just released. */
	dataset: v.optional(v.nullable(v.string())),
});
export type Decision = v.InferOutput<typeof DecisionSchema>;

/** A reason code rendered for a person, rather than shown raw.
 *
 * Falls through to the raw code rather than hiding an unknown one: a reason this build cannot name
 * is still the reason a validator is being asked, and swallowing it would leave them approving
 * something unexplained. */
export function reasonLabel(code: string): string {
	if (code === 'first_promotion') return 'First promotion of this dataset';
	if (code === 'row_count_delta') return 'Row count moved outside the review band';
	return code;
}
