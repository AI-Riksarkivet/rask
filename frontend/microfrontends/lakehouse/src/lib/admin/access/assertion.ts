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

/** One stored relationship tuple — a FACT the assertion depends on, not a claim about it. */
export type TupleFact = {
	user: string;
	relation: string;
	object: string;
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
export function toFgaYaml(
	name: string,
	assertions: readonly Assertion[],
	/**
	 * The stored tuples the verdict rests on, emitted as a PER-TEST `tuples:` block.
	 *
	 * Without these the export is a trap. `fga model test` never contacts the OpenFGA server — it
	 * evaluates the model against the fixture written in the YAML, in memory. The verdict you just saw
	 * came from the LIVE store (Postgres), so a subject that exists there and not in the checked-in
	 * fixture — every real OIDC `sub`, which is exactly what you would be debugging — pastes in as a
	 * test that fails immediately. Verified: a live Dex sub exported without its tuples gives
	 * `expected=true, got=false` and exit 1.
	 *
	 * A test-local `tuples:` block layers on top of the file's global fixture (verified against the
	 * CLI), so the pasted block is self-contained and does not require editing the shared fixture.
	 */
	tuples: readonly TupleFact[] = [],
): string {
	if (assertions.length === 0) return '';
	const grouped = new Map<string, { user: string; object: string; rels: Map<string, boolean> }>();
	for (const a of assertions) {
		const key = `${a.user} ${a.object}`;
		const entry = grouped.get(key) ?? { user: a.user, object: a.object, rels: new Map() };
		entry.rels.set(a.relation, a.allowed);
		grouped.set(key, entry);
	}

	const lines = ['tests:', `  - name: ${name}`];
	if (tuples.length > 0) {
		lines.push('    tuples:');
		for (const t of tuples) {
			lines.push(`      - user: ${scalar(t.user)}`);
			lines.push(`        relation: ${t.relation}`);
			lines.push(`        object: ${scalar(t.object)}`);
		}
	}
	lines.push('    check:');
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
