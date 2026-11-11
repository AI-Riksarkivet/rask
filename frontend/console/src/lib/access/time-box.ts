import { formatZoned } from '@rask/ui/utils';
import type { TupleCondition } from '../access';

// What a time-boxed grant's condition says about its window. The compiled model types each of a
// condition's parameters (`GET /management/v1/access/model`), and the window is read by those types: the
// `duration` parameter is how long it lasts, the `timestamp` one other than the checker's
// `current_time` is when it opened. No condition or parameter name is kept here.

/** `{condition: {parameter: type}}`, as the model serves it. */
export type ModelConditions = Record<string, Record<string, string>>;

const UNIT_MS: Record<string, number> = { h: 3_600_000, m: 60_000, s: 1_000, ms: 1 };

/** Milliseconds in a Go duration (`1h`, `90m`, `1h30m`, `1.5h`), or `null` for text that is not one. */
export function durationMs(text: string): number | null {
	const parts = [...text.matchAll(/(\d+(?:\.\d+)?)(ms|h|m|s)/g)];
	if (parts.length === 0 || parts.map((p) => p[0]).join('') !== text) return null;
	return parts.reduce(
		(sum, [, amount, unit]) => sum + Number(amount) * (UNIT_MS[unit ?? ''] ?? 0),
		0,
	);
}

/** The parameter of `type` in `params`, leaving out the checker's clock. */
const parameterOf = (params: Record<string, string>, type: string): string | undefined =>
	Object.entries(params).find(([name, t]) => t === type && name !== 'current_time')?.[0];

/** "for 1h, until 2026-11-11 21:30:26 UTC+01:00": a condition's window in words, or `null` when the
 *  grant is permanent or its condition takes no duration. `until` is left out when the window's start
 *  is not a readable moment. */
export function timeBoxLabel(
	condition: TupleCondition | null | undefined,
	conditions: ModelConditions,
): string | null {
	if (!condition) return null;
	const params = conditions[condition.name];
	if (!params) return null;
	const spanParam = parameterOf(params, 'duration');
	const duration = spanParam === undefined ? undefined : condition.context[spanParam];
	if (typeof duration !== 'string') return null;
	const startParam = parameterOf(params, 'timestamp');
	const start = startParam === undefined ? undefined : condition.context[startParam];
	const opened = typeof start === 'string' ? Date.parse(start) : Number.NaN;
	const ms = durationMs(duration);
	return Number.isNaN(opened) || ms === null
		? `for ${duration}`
		: `for ${duration}, until ${formatZoned(opened + ms)}`;
}
