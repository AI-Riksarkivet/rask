import { command, query } from '#lib/reads.svelte.ts';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import {
	PromotionDecisionSchema,
	PromotionsPageSchema,
	type PromotionDecision,
	type PromotionHold,
	type PromotionsPage,
} from '../promotions';
import { catalogJSON, parsed } from '#lib/doors.ts';

// The held promotions, on the catalog's publication doors. The list is every open hold the caller may
// see (`can_get_metadata` on its table), one page at a time. A decision is gated on `can_promote` and
// carries the held VERSION: the catalog refuses (409) a version it does not hold, one decided otherwise,
// or one `published` already names, so a stale page cannot approve something else.

const enc = encodeURIComponent;

/** One page of open holds; `pageToken` continues after the previous page's last table. */
export const listPromotions = query(
	v.object({ pageToken: v.optional(v.string()) }),
	async ({ pageToken }): Promise<ApiResult<PromotionsPage>> => {
		const search = new URLSearchParams({ limit: '100' });
		if (pageToken) search.set('page_token', pageToken);
		return parsed(await catalogJSON(`/management/v1/promotions?${search}`), PromotionsPageSchema);
	},
);

/** The open hold on `table`, read off the list (the catalog serves no per-table hold door): `null`
 *  when no open hold names it. */
export const getPromotion = query(
	v.object({ table: v.string() }),
	async ({ table }): Promise<ApiResult<PromotionHold | null>> => {
		let pageToken: string | undefined;
		for (;;) {
			const page = await listPromotions({ pageToken });
			if (!page.ok) return page;
			const hold = page.data.holds.find((h) => h.table === table);
			if (hold) return { ok: true, data: hold };
			if (!page.data.page_token) return { ok: true, data: null };
			pageToken = page.data.page_token;
		}
	},
);

/** Approve (`published` moves to `version`) or reject (`main` is restored to what `published` names). */
export const decidePromotion = command(
	({ table, version, approve }) =>
		`${approve ? 'Approve' : 'Reject'} the promotion of table ${table} to version ${version}`,
	v.object({ table: v.string(), version: v.number(), approve: v.boolean() }),
	async ({ table, version, approve }): Promise<ApiResult<PromotionDecision>> => {
		const verb = approve ? 'approve' : 'reject';
		const result = parsed(
			await catalogJSON(`/management/v1/table/${enc(table)}/promotion/${verb}`, {
				method: 'POST',
				body: JSON.stringify({ version }),
			}),
			PromotionDecisionSchema,
		);
		void listPromotions({}).refresh();
		void getPromotion({ table }).refresh();
		return result;
	},
);
