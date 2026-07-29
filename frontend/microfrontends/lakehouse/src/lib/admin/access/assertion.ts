// Turn a checked verdict into a `.fga.yaml` test — the step that makes a finding permanent.
//
// Running a check answers "is this true right now". Committing the same check into `model.fga.yaml`
// answers "can this ever stop being true", because `fga model test` runs that file in CI (`ms-authz`).
// Without an export, the loop from "I found a surprising grant" to "this can never regress" is
// hand-written YAML — which is exactly the friction that stops it happening.
//
// The emitted shape mirrors the blocks already in `model.fga.yaml`: `tests[].check[]` with an inline
// `assertions` flow map, two-space indent. Deliberately NOT a YAML library — this is one known shape in
// a file people hand-maintain, and matching its house style matters more than generality.

export type Assertion = {
	user: string;
	object: string;
	relation: string;
	allowed: boolean;
};

/** A YAML double-quoted scalar. Ids here are `type:id` and relations are `[a-z_]+`, so the only
 *  characters that realistically need it are the quote and backslash — handled rather than assumed
 *  away, because an id is user-supplied text and a silently broken export is worse than none. */
const scalar = (value: string): string => `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;

/**
 * Render verdicts as a `tests:` block.
 *
 * Grouped by subject+object, so a subject checked against several relations on one object produces ONE
 * `check` entry with a combined `assertions` map. That is how the existing file is written, and it
 * reads as a statement about that subject rather than as repetition.
 */
export function toFgaYaml(name: string, assertions: readonly Assertion[]): string {
	if (assertions.length === 0) return '';
	const grouped = new Map<string, { user: string; object: string; rels: Map<string, boolean> }>();
	for (const a of assertions) {
		const key = `${a.user} ${a.object}`;
		const entry = grouped.get(key) ?? { user: a.user, object: a.object, rels: new Map() };
		entry.rels.set(a.relation, a.allowed);
		grouped.set(key, entry);
	}

	const lines = ['tests:', `  - name: ${name}`, '    check:'];
	for (const { user, object, rels } of grouped.values()) {
		const map = [...rels].map(([r, ok]) => `${r}: ${ok}`).join(', ');
		lines.push(`      - user: ${scalar(user)}`);
		lines.push(`        object: ${scalar(object)}`);
		lines.push(`        assertions: { ${map} }`);
	}
	return `${lines.join('\n')}\n`;
}

/** A stable, human name for the test block, derived from what was actually asserted. */
export function assertionName(a: Assertion): string {
	return `${a.user} ${a.allowed ? 'holds' : 'does NOT hold'} ${a.relation} on ${a.object}`;
}
