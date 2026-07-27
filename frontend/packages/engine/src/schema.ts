// Annotation TYPE vocabulary (statuses, shapes, relations, region classes).
//
// NOTE: the parallel ANNOTATION_COLUMNS / PAGE_COLUMNS lists were removed 2026-07-21 —
// they were dead (no consumer) and already drifted (missing t_start/t_end, ra-anno's
// page_id identity). The SINGLE schema source of truth is the backend's EMPTY_SCHEMA
// (services/annotator/annotations/schema.py); the engine is schema-driven at runtime —
// ArrowDataPlugin reads columns by name from whatever table the wire delivers.

export type AnnotationStatus = 'prediction' | 'draft' | 'reviewed' | 'accepted' | 'rejected';

export type AnnotationSource = 'manual' | `model:${string}`;

/**
 * Geometry kind. Unified model: every shape is rings of points + flags.
 * Axis-aligned/oriented boxes also store x/y/width/height(/rotation) for fast bbox ops;
 * polygon holds flat [x,y,...] coords. 'baseline'/'line' are OPEN rings (not closed).
 */
export type ShapeType =
	| 'rectangle' // axis-aligned box
	| 'rotation' // oriented box (uses rotation, radians)
	| 'polygon' // closed ring
	| 'baseline' // open polyline — the HTR text-line baseline
	| 'line' // open 2+ point line
	| 'point' // single point
	| 'mask'; // raster mask (base64 in `mask` column) — from the brush tool

export const SHAPE_TYPES: readonly ShapeType[] = [
	'rectangle',
	'rotation',
	'polygon',
	'baseline',
	'line',
	'point',
	'mask',
] as const;

/** Relation/link between annotations (X-AnyLabeling kie_linking). Stored JSON-encoded in `links`. */
export interface AnnotationRelation {
	to: string; // target annotation id
	type: RelationType;
}

export type RelationType =
	| 'key-value' // form key → value
	| 'continued' // line/region continues in another
	| 'reading-next' // explicit reading-order successor
	| 'parent' // child → parent region (region→line→word)
	| 'reference'; // marginalia/footnote → referent

/**
 * Suggested controlled region/line vocabulary (PAGE-XML / SegmOnto-flavored).
 * `label` may use these or stay free-text — NOT enforced by the schema.
 */
export const REGION_TYPES = [
	'paragraph',
	'heading',
	'text-line',
	'word',
	'marginalia',
	'page-number',
	'header',
	'footer',
	'table',
	'table-cell',
	'figure',
	'caption',
	'signature',
	'stamp-seal',
	'formula',
	'catch-word',
] as const;

export type RegionType = (typeof REGION_TYPES)[number];
