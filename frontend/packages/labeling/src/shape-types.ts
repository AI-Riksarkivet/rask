/** The ONE shape vocabulary, and the translation from the engine's tool names into it.
 *
 * THE BUG THIS EXISTS TO KILL. The PixiJS engine names its committed shapes after its TOOLS —
 * `rect`, `point`, `line`, `polygon`, `mask` — while the annotator service types a draft against
 * `bbox | polygon | mask | segment | tag | text`. Only `polygon` and `mask` appear in both, so five
 * of the seven drawing tools produced a draft the service could not accept, and the shipped default
 * template preset (`bbox-detection`, tools `['bbox']`) declared a tool NO canvas tool could emit —
 * an out-of-the-box project that was unsatisfiable by construction.
 *
 * There WAS a translation, and it made things worse rather than better: the canvas mapped
 * `rect -> 'rectangle'`, a third name accepted by neither side. Someone looked at this seam, saw
 * the mismatch, and mapped to a target that exists nowhere.
 *
 * It survived a green test suite because `draft-sync.test.ts` fed itself the literal `'bbox'` —
 * a value the canvas has never produced — so the test proved only that the function copies a string.
 * A fixture that invents its input cannot fail the way production does.
 *
 * Direction of the fix: the SERVICE vocabulary is canonical, because it is what the draft, the
 * ontology, the published Arrow table and the lineage facet all speak. The engine keeps its
 * tool-shaped names (they are correct for a drawing tool) and this module is the single seam that
 * maps them. One place to look, one place to change.
 */

/** The canonical set — mirrors `ShapeType` in the annotator service (models.py / ontology.py). */
export const SHAPE_TYPES = [
	'bbox',
	'polygon',
	'mask',
	'keypoint',
	'polyline',
	'segment',
	'tag',
	'text',
] as const;

export type ShapeType = (typeof SHAPE_TYPES)[number];

/** Engine tool-name -> canonical shape type. Only the names that DIFFER need an entry; anything
 *  already canonical passes through, which keeps `polygon`/`mask`/`segment` a no-op. */
const FROM_ENGINE: Record<string, ShapeType> = {
	rect: 'bbox',
	// The pre-fix canvas emitted this. Kept so drafts already persisted in actor state — and rows
	// already written to the annotations table — still normalize instead of being silently dropped.
	rectangle: 'bbox',
	point: 'keypoint',
	line: 'polyline',
	// A pencil stroke committed with Shift is an open baseline, which is a polyline, not a region.
	baseline: 'polyline',
};

/** Normalize whatever the engine committed into the canonical vocabulary.
 *
 *  Returns `null` for anything unrecognised rather than passing it through: an unknown shape type
 *  reaching the service is refused at submit with a message about tools, which reads as a template
 *  problem and sends the reader to the wrong place entirely. Callers drop the row and can say so.
 */
export function canonicalShapeType(engineType: string | null | undefined): ShapeType | null {
	if (!engineType) return null;
	const mapped = FROM_ENGINE[engineType];
	if (mapped) return mapped;
	return (SHAPE_TYPES as readonly string[]).includes(engineType) ? (engineType as ShapeType) : null;
}

/** True when the engine can actually DRAW this canonical type today.
 *
 *  `text` has no tool (the text-span/NER editor is unbuilt) and `tag` is applied from the sidebar
 *  rather than drawn. A task template that demands them cannot be satisfied on the canvas, so the
 *  create dialog should not offer them as if it could.
 */
export const DRAWABLE: readonly ShapeType[] = [
	'bbox',
	'polygon',
	'mask',
	'keypoint',
	'polyline',
	'segment',
];

/** Canonical shape type -> the ENGINE tool names that produce it.
 *
 *  The inverse of {@link canonicalShapeType}, and it is one-to-many: a polygon can come from the
 *  polygon tool, the magnetic corner-snap tool, or a brush stroke vectorized on release. A task
 *  that permits `polygon` must therefore offer all three, or the restriction reads as three broken
 *  buttons rather than one honest constraint.
 *
 *  `select`, `pan` and `lasso` are deliberately absent: they navigate and select, they do not
 *  commit a shape, so no task restriction should ever hide them.
 */
const TOOLS_FOR: Record<ShapeType, readonly string[]> = {
	bbox: ['rect'],
	polygon: ['polygon', 'magnetic', 'brush', 'pencil'],
	mask: ['brush'],
	keypoint: ['point'],
	polyline: ['line', 'pencil'],
	// Not drawn on the canvas: a segment is dragged on the waveform lane, a tag is applied from the
	// sidebar, and no text-span editor exists yet.
	segment: [],
	tag: [],
	text: [],
};

/** Every engine tool that COMMITS a shape — the union of the map above.
 *
 *  A task restriction may only ever hide one of these. `lasso` is the reason this exists: the tool
 *  registry flags it `drawing: true` (it is a canvas gesture) but its own label says "Lasso
 *  (select)" — it commits nothing. Filtering on "absent from the map" hid it, because absence there
 *  means "produces no shape", not "produces a forbidden shape". Those are opposite meanings and the
 *  registry's single `drawing` flag cannot tell them apart. */
export const COMMITS_SHAPE: ReadonlySet<string> = new Set(Object.values(TOOLS_FOR).flat());

/** The engine tools a task permitting `allowed` should offer.
 *
 *  An EMPTY `allowed` means unconstrained — return null so the caller shows everything, rather than
 *  an empty array which would hide every drawing tool and look like a broken toolbar. */
export function engineToolsFor(allowed: readonly string[] | null | undefined): Set<string> | null {
	if (!allowed || allowed.length === 0) return null;
	const out = new Set<string>();
	for (const type of allowed) {
		for (const tool of TOOLS_FOR[type as ShapeType] ?? []) out.add(tool);
	}
	// A task that permits only non-drawable types (tag-only classification, say) still must not
	// present an empty rail as if the canvas were broken.
	return out.size > 0 ? out : null;
}
