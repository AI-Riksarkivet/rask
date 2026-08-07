// The engine's REVIEW-status vocabulary — the one piece of annotation vocabulary the canvas itself
// consumes (`utils/color.ts` maps it to stroke colours).
//
// Everything else that used to live here (ShapeType/SHAPE_TYPES, AnnotationSource, the relation
// types, REGION_TYPES) was a SHADOW of `@rask/labeling/shape-types` — the canonical, service-
// mirrored vocabulary with the engine→canonical mapping — and had ZERO consumers in the engine or
// the estate (#96; the parallel ANNOTATION_COLUMNS lists died the same death 2026-07-21). The
// engine is schema-driven at runtime: ArrowDataPlugin reads columns by name from whatever table
// the wire delivers, so the single source of truth stays the backend's EMPTY_SCHEMA
// (services/annotator/annotations/schema.py).

export type AnnotationStatus = 'prediction' | 'draft' | 'reviewed' | 'accepted' | 'rejected';
