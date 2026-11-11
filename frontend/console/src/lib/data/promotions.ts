/**
 * A HELD PROMOTION — a version a project's quality gate found unusual (not broken), waiting for a
 * `can_promote` holder to approve it (`published` moves to it) or reject it (`main` is restored to
 * what `published` names). The shapes are the catalog's generated contract; `satisfies` keeps each
 * schema's keys in lockstep with it, so a field the catalog adds or renames stops compiling here.
 */
import * as v from 'valibot';
import type { components } from '#lib/api/generated/catalog.ts';

type HoldWire = components['schemas']['PromotionHold'];
type ListWire = components['schemas']['PromotionsListResponse'];
type DecisionWire = components['schemas']['PromotionDecision'];

/** One hold, as `GET /management/v1/promotions` lists it: the table as it is named now, the held
 *  version, what `published` names, why it is held and when the sweep expires it. */
export const PromotionHoldSchema = v.object({
	table: v.string(),
	uuid: v.string(),
	version: v.number(),
	published_version: v.nullable(v.number()),
	reasons: v.array(v.string()),
	opened_at: v.string(),
	expires_at: v.string(),
	requested_by: v.optional(v.nullable(v.string())),
	state: v.optional(v.picklist(['open', 'approved', 'rejected', 'expired', 'superseded'])),
	decided_at: v.optional(v.nullable(v.string())),
	decided_by: v.optional(v.nullable(v.string())),
	restored_version: v.optional(v.nullable(v.number())),
} satisfies Record<keyof HoldWire, unknown>);
export type PromotionHold = v.InferOutput<typeof PromotionHoldSchema>;

export const PromotionsPageSchema = v.object({
	holds: v.array(PromotionHoldSchema),
	page_token: v.optional(v.nullable(v.string())),
} satisfies Record<keyof ListWire, unknown>);
export type PromotionsPage = v.InferOutput<typeof PromotionsPageSchema>;

/** What an approve or a reject answers: the decided hold, and where `published` (or `main`) now stands. */
export const PromotionDecisionSchema = v.object({
	hold: PromotionHoldSchema,
	published_version: v.nullable(v.number()),
	restored_version: v.optional(v.nullable(v.number())),
} satisfies Record<keyof DecisionWire, unknown>);
export type PromotionDecision = v.InferOutput<typeof PromotionDecisionSchema>;

/** A reason code rendered for a person, rather than shown raw. The band's two reasons are named; a
 *  failed quality assertion the gate holds rather than refuses is shown by its own name. */
export function reasonLabel(code: string): string {
	if (code === 'first_publication') return 'Nothing is published yet — this would be the first';
	if (code === 'row_count_delta') return 'Row count moved outside the review band';
	return code;
}
