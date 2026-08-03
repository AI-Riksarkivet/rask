// The SCHEMA graph — types and relations, with no tuples involved.
//
// The data graph next door answers "who has access to what right now". This answers "what CAN grant
// what, ever" — a property of the model, true before a single tuple exists. Both are needed and neither
// substitutes: a cascade you cannot see is the reason a surprising grant is surprising.
//
// Written because the reference implementations get this wrong in a specific, checkable way. OpenFGA's
// own Types Previewer walks `type_definitions` and emits edges for `computedUserset` only — it draws
// NO edge for `tupleToUserset` (`X from Y`) and none for `intersection` or `difference` children. Its
// maintainers say so (openfga/openfga discussion #299: "our current visualization predates the 1.1
// schema"). In THIS model `X from Y` is the entire containment cascade — team → project → warehouse →
// namespace → table — so a previewer that omits it draws a model with the spine removed.
//
// Parsed from the DSL text rather than the compiled JSON on purpose: the DSL is what a human edits and
// what `GET /v1/access/model` already serves, so the picture is of the file you would change.

import type { BuiltGraph, GraphEdge, GraphNode } from './graph';

/** How one relation is satisfied. The operator IS the information — flattening these to a plain edge
 *  is precisely what makes an excluded subject look granted. */
export type RelationEdgeKind =
	| 'assignable' // `[user]` — a tuple can be written directly
	| 'implies' // `or writer` — a same-object rung (computedUserset)
	| 'from-parent' // `or owner from parent` — tupleToUserset, the cascade
	| 'requires' // `and validator` — intersection
	| 'excludes'; // `but not banned` — difference

const KIND_LABEL: Record<RelationEdgeKind, string> = {
	assignable: 'assignable',
	implies: 'implied by',
	'from-parent': 'from',
	requires: 'and',
	excludes: 'but not',
};

const relationEdgeLabel = (kind: RelationEdgeKind): string => KIND_LABEL[kind];

export type ParsedRelation = {
	name: string;
	/** Types a direct tuple may name, e.g. `['user', 'role#assignee']`. Empty ⇒ derived only. */
	assignable: string[];
	/** Same-object rungs this relation inherits from (`or owner`). */
	implies: string[];
	/** `[relation, viaRelation]` pairs — `owner from parent` is `['owner', 'parent']`. */
	fromParent: [string, string][];
	/** Intersection operands (`and`). */
	requires: string[];
	/** Difference operands (`but not`). */
	excludes: string[];
	/** Conditions a direct tuple may carry — `[user with non_expired_grant]`. */
	conditions: string[];
};

export type ParsedType = {
	name: string;
	relations: ParsedRelation[];
};

const EMPTY_RELATION = (name: string): ParsedRelation => ({
	name,
	assignable: [],
	implies: [],
	fromParent: [],
	requires: [],
	excludes: [],
	conditions: [],
});

/**
 * Parse the OpenFGA DSL into types + relations.
 *
 * Hand-written rather than pulled from a parser dependency: the estate already regex-scrapes `type`
 * lines out of this same text (`parseModelTypes`), the grammar in play is a handful of line shapes,
 * and a 1 MB ANTLR grammar to draw a picture is not a trade this bundle makes. It is deliberately
 * TOLERANT — an unrecognised clause is skipped, never thrown on, because a model that gains syntax
 * should degrade to a slightly-incomplete diagram and not a blank page with an error.
 */
function parseModel(dsl: string): ParsedType[] {
	const types: ParsedType[] = [];
	let current: ParsedType | null = null;

	for (const raw of dsl.split('\n')) {
		// Strip comments, but ONLY where `#` starts one — after whitespace or at line start. A naive
		// `split('#')` truncates `[user, role#assignee, …]` at the userset, which silently emptied every
		// relation that accepts a role and made the whole graph look like it had no assignable rungs.
		const line = raw.replace(/(^|\s)#.*$/, '$1').trimEnd();
		const typeMatch = /^type\s+(\S+)\s*$/.exec(line.trim());
		if (typeMatch?.[1]) {
			current = { name: typeMatch[1], relations: [] };
			types.push(current);
			continue;
		}
		// A `condition` block ends the type section; nothing after it is a relation.
		if (/^condition\s+/.test(line.trim())) {
			current = null;
			continue;
		}
		const defineMatch = /^\s*define\s+([A-Za-z0-9_]+)\s*:\s*(.+)$/.exec(line);
		if (!defineMatch || !current) continue;

		const [, name, body] = defineMatch;
		if (!name || !body) continue;
		const relation = EMPTY_RELATION(name);

		// `[user, role#assignee, user with non_expired_grant]` — the direct-assignment bracket.
		const bracket = /\[([^\]]*)\]/.exec(body);
		if (bracket?.[1]) {
			for (const entry of bracket[1]
				.split(',')
				.map((e) => e.trim())
				.filter(Boolean)) {
				const [subject, condition] = entry.split(/\s+with\s+/).map((x) => x.trim());
				if (!subject) continue;
				if (!relation.assignable.includes(subject)) relation.assignable.push(subject);
				if (condition && !relation.conditions.includes(condition))
					relation.conditions.push(condition);
			}
		}

		// Everything after the bracket is the rewrite expression.
		const rest = body.replace(/\[[^\]]*\]/, ' ');

		// `but not X` first — it binds the operand that follows it, and scanning for `or`/`and` before
		// removing it would file an EXCLUSION under whichever operator happened to precede it.
		for (const m of rest.matchAll(/\bbut\s+not\s+([A-Za-z0-9_]+)/g)) {
			if (m[1] && !relation.excludes.includes(m[1])) relation.excludes.push(m[1]);
		}
		const withoutExclusions = rest.replace(/\bbut\s+not\s+[A-Za-z0-9_]+/g, ' ');

		// `X from Y` — the cascade. Matched BEFORE the bare-rung scan so `owner from parent` is not
		// also filed as a same-object `implies: owner`, which would draw a self-loop that does not exist.
		for (const m of withoutExclusions.matchAll(/\b([A-Za-z0-9_]+)\s+from\s+([A-Za-z0-9_]+)/g)) {
			if (m[1] && m[2]) relation.fromParent.push([m[1], m[2]]);
		}
		const withoutFrom = withoutExclusions.replace(/\b[A-Za-z0-9_]+\s+from\s+[A-Za-z0-9_]+/g, ' ');

		// `and X` → intersection; a bare rung after `or` (or alone) → implication.
		for (const m of withoutFrom.matchAll(/\band\s+([A-Za-z0-9_]+)/g)) {
			if (m[1] && !relation.requires.includes(m[1])) relation.requires.push(m[1]);
		}
		const withoutAnd = withoutFrom.replace(/\band\s+[A-Za-z0-9_]+/g, ' ');
		for (const token of withoutAnd.split(/\bor\b/).map((t) => t.trim())) {
			const m = /^([A-Za-z0-9_]+)$/.exec(token);
			if (m?.[1] && !relation.implies.includes(m[1])) relation.implies.push(m[1]);
		}

		current.relations.push(relation);
	}
	return types;
}

/** Node id for a type, and for one of its relations. Namespaced so a type named like a relation cannot
 *  collide with it. */
const typeNodeId = (type: string): string => `type:${type}`;
const relationNodeId = (type: string, relation: string): string => `rel:${type}.${relation}`;

/**
 * Lay the parsed model out as a graph.
 *
 * Types stack in one column, their relations in the next (see the note in the body for why not one
 * column per type). Edges carry the OPERATOR, so `owner from parent` draws as a labelled hop to the
 * parent type's relation rather than vanishing — the omission that makes the official previewer unable
 * to show this model's spine.
 */
export function buildModelGraph(dsl: string): BuiltGraph {
	const parsed = parseModel(dsl);
	const nodes = new Map<string, GraphNode>();
	const edges: GraphEdge[] = [];
	const byType = new Map(parsed.map((t) => [t.name, t]));

	// TWO columns, not one per type: every type in layer 0, every relation in layer 1, stacked. A
	// column-per-type layout is the obvious reading of "types are columns" and it is wrong at this
	// model's size — ten types at ~550 px apart is a 5.5 k-pixel canvas, and `fitView` answers that by
	// zooming out until nothing is legible. The layered placer's barycentre pass keeps each type
	// vertically near the relations that point at it, which is the alignment that actually helps.
	parsed.forEach((type) => {
		nodes.set(typeNodeId(type.name), {
			id: typeNodeId(type.name),
			fgaType: type.name,
			label: type.name,
			role: 'container',
			depth: 0,
			via: 'type',
		});
		for (const relation of type.relations) {
			const id = relationNodeId(type.name, relation.name);
			nodes.set(id, {
				id,
				fgaType: type.name,
				label: relation.name,
				// A relation nobody can write a tuple for is DERIVED — it is computed by the model. Marking
				// it distinctly is the schema-side twin of the grant dialog refusing to offer a `can_*`.
				role: relation.assignable.length ? 'subject' : 'dim',
				depth: 1,
				via: relation.conditions.length ? `conditional (${relation.conditions.join(', ')})` : null,
			});
			edges.push({
				id: `${typeNodeId(type.name)}->${id}`,
				source: typeNodeId(type.name),
				target: id,
				label: '',
				onPath: false,
			});

			const link = (target: string, kind: RelationEdgeKind) => {
				const edgeId = `${id}->${target}:${kind}`;
				if (edges.some((e) => e.id === edgeId)) return;
				edges.push({
					id: edgeId,
					source: target,
					target: id,
					label: relationEdgeLabel(kind),
					onPath: kind === 'from-parent',
					condition: kind === 'excludes' ? 'excludes' : null,
				});
			};

			for (const other of relation.implies) {
				if (byType.get(type.name)?.relations.some((r) => r.name === other)) {
					link(relationNodeId(type.name, other), 'implies');
				}
			}
			for (const other of relation.requires) {
				if (byType.get(type.name)?.relations.some((r) => r.name === other)) {
					link(relationNodeId(type.name, other), 'requires');
				}
			}
			for (const other of relation.excludes) {
				if (byType.get(type.name)?.relations.some((r) => r.name === other)) {
					link(relationNodeId(type.name, other), 'excludes');
				}
			}
			// The cascade. `owner from parent` means: follow this type's `parent` relation to whatever it
			// points at, and read `owner` THERE. So the edge lands on the target type's own relation.
			for (const [wanted, via] of relation.fromParent) {
				const viaRelation = byType.get(type.name)?.relations.find((r) => r.name === via);
				for (const targetType of viaRelation?.assignable ?? []) {
					const bare = targetType.split('#')[0] ?? targetType;
					if (byType.get(bare)?.relations.some((r) => r.name === wanted)) {
						link(relationNodeId(bare, wanted), 'from-parent');
					}
				}
			}
		}
	});

	const nodeList = [...nodes.values()];
	const typeCounts = new Map<string, number>();
	for (const n of nodeList) typeCounts.set(n.fgaType, (typeCounts.get(n.fgaType) ?? 0) + 1);
	const relationCounts = new Map<string, number>();
	for (const e of edges) {
		if (!e.label) continue;
		relationCounts.set(e.label, (relationCounts.get(e.label) ?? 0) + 1);
	}
	return { nodes: nodeList, edges, typeCounts, relationCounts };
}
