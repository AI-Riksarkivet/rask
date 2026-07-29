/**
 * ra-anno engine — public API.
 *
 * Framework-agnostic annotation + rendering engine: PixiJS (WebGPU/WebGL) rendering,
 * Apache Arrow data layer, interaction/tools/editors, geometry, mask ops, and the
 * annotation schema. Imports ZERO Svelte / `$app` — consumed by the thin Svelte
 * binding and the app — graduated to the standalone @rask/engine package.
 */

// ── Rendering (PixiJS plugins + context types) ──
export * from './pixi/ImagePlugin.js';
export * from './pixi/ArrowDataPlugin.js';
export * from './pixi/types.js';

// ── Interaction layer ──
export * from './interaction/InteractionManager.js';
export * from './interaction/geometry.js';
export * from './interaction/types.js';

// ── Drawing tools ──
export * from './tools/RectTool.js';
export * from './tools/PolygonTool.js';
export * from './tools/LassoTool.js';
export * from './tools/MagneticTool.js';
export * from './tools/corners.js';
export * from './tools/PencilTool.js';
export * from './tools/PointTool.js';
export * from './tools/LineTool.js';
export * from './tools/BrushTool.js';

// ── Shape editors ──
export * from './editors/RectEditor.js';
export * from './editors/PolygonEditor.js';

// ── Framework-agnostic state stores + Arrow batch construction ──
// NOTE: a 750-line AnnotationStore + transport seam (a parallel local-first overlay the
// runes controller never adopted) was removed 2026-07-21 — the controller IS that layer.
// buildBatchTable (its one used export) lives on in store/batch.ts.
export * from './store/batch.js';
export * from './store/LayerStore.js';

// ── Temporal surface (audio waveform — the one Canvas2D lane; video reuses ImagePlugin) ──
export * from './temporal/WaveSurface.js';

// ── Data model + utilities ──
export * from './schema.js';
export * from './utils/arrow.js';
export * from './utils/color.js';
export * from './maskOps.js';

// The public `Tool` is the tool-id union from ./pixi/types; the engine-internal `Tool`
// *interface* in ./interaction/types is imported directly by tool classes, not re-exported.
export type { Tool } from './pixi/types.js';
