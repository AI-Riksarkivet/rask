// The access view's three questions, asked of the catalog's access doors: "what can this subject do"
// (list-objects), "who can do this" (list-users) and "why" (check and expand together). Each answer is
// returned as what the access view draws: the answer graph, the verdict, the chain of words beside it,
// the names the catalog gave the uuid objects it answered about, and the node to seed and select.

import { namedObject } from '../access';
import { checkAccess, expand, listObjects, listUsers } from '../remote/access.api';
import { type BuiltGraph, buildNeighbourhood, type QueryKind, walkDerivation } from './graph';

export type Verdict = { allowed: boolean; user: string; relation: string; object: string };

export type QueryInput = {
	user: string;
	relation: string;
	object: string;
	objectType: string;
	depth: number;
};

export type QueryOutcome =
	| { ok: false; status: number; detail: string }
	| {
			ok: true;
			/** The names the answer gave its table and namespace objects (`table:<uuid>` → `db$t`). */
			names: ReadonlyMap<string, string>;
			answer: BuiltGraph | null;
			verdict: Verdict | null;
			chain: string[];
			/** The node the canvas centres on, or `null` to keep the current one. */
			seed: string | null;
			selected: string;
			/** Said beside a verdict the store gave without its derivation. */
			note: string | null;
	  };

/**
 * Ask one question. `storeNames` are the names the tuple store gave its objects; they win over the
 * answer's, as they are the newer read of the same objects.
 */
export async function askAccess(
	kind: QueryKind,
	input: QueryInput,
	storeNames: ReadonlyMap<string, string>,
): Promise<QueryOutcome> {
	const shownWith =
		(names: ReadonlyMap<string, string>) =>
		(id: string): string =>
			namedObject(id, storeNames.get(id) ?? names.get(id));
	if (kind === 'what') return askWhat(input);
	if (kind === 'who') return askWho(input, shownWith);
	return askWhy(input, shownWith);
}

async function askWhat(input: QueryInput): Promise<QueryOutcome> {
	const res = await listObjects({
		user: input.user.trim(),
		relation: input.relation.trim(),
		type: input.objectType.trim(),
	});
	if (!res.ok) return res;
	const parsed = res.data;
	return {
		ok: true,
		names: new Map(Object.entries(parsed.names)),
		answer: buildNeighbourhood(
			parsed.user,
			parsed.objects.map((o) => ({ user: parsed.user, relation: parsed.relation, object: o })),
		),
		verdict: null,
		chain: [
			`${parsed.user} holds ${parsed.relation} on ${parsed.objects.length} ${parsed.type} object(s)`,
		],
		seed: parsed.user,
		selected: parsed.user,
		note: null,
	};
}

type ShownWith = (names: ReadonlyMap<string, string>) => (id: string) => string;

async function askWho(input: QueryInput, shownWith: ShownWith): Promise<QueryOutcome> {
	const res = await listUsers({ object: input.object.trim(), relation: input.relation.trim() });
	if (!res.ok) return res;
	const parsed = res.data;
	const names = parsed.name ? new Map([[parsed.object, parsed.name]]) : new Map<string, string>();
	return {
		ok: true,
		names,
		answer: buildNeighbourhood(
			parsed.object,
			parsed.users.map((u) => ({ user: u, relation: parsed.relation, object: parsed.object })),
		),
		verdict: null,
		chain: [
			`${parsed.users.length} subject(s) hold ${parsed.relation} on ${shownWith(names)(parsed.object)}` +
				(parsed.truncated ? ' — TRUNCATED by the store, the real number is higher' : ''),
		],
		seed: parsed.object,
		selected: parsed.object,
		note: null,
	};
}

async function askWhy(input: QueryInput, shownWith: ShownWith): Promise<QueryOutcome> {
	// Check AND Expand together: the verdict is the fact, the tree is the explanation. Either alone
	// is a dead end.
	const triple = {
		user: input.user.trim(),
		relation: input.relation.trim(),
		object: input.object.trim(),
	};
	const [checkRes, expandRes] = await Promise.all([
		checkAccess(triple),
		expand({ object: triple.object, relation: triple.relation, depth: input.depth }),
	]);
	if (!checkRes.ok) return checkRes;
	const probed = checkRes.data;
	// The catalog resolved a name to the uuid object it checked, so the verdict, the selection and the
	// derivation all name that object, and the name rides beside it.
	const names = probed.checked.name
		? new Map([[probed.checked.object, probed.checked.name]])
		: new Map<string, string>();
	const verdict: Verdict = {
		allowed: probed.allowed,
		user: probed.checked.user,
		relation: probed.checked.relation,
		object: probed.checked.object,
	};
	const base = { ok: true as const, names, verdict, selected: probed.checked.object };
	if (!expandRes.ok) {
		return {
			...base,
			answer: null,
			chain: [],
			seed: null,
			note: 'The verdict is live, but the derivation could not be read.',
		};
	}
	const parsed = expandRes.data;
	const answer = walkDerivation(parsed.tree, parsed.object, parsed.relation);
	const shown = shownWith(names);
	return {
		...base,
		answer,
		chain: answer.edges
			.filter((e) => e.onPath)
			.map((e) => `${shown(e.source)} → ${shown(e.target)} (${e.label})`),
		seed: parsed.object,
		note: null,
	};
}
