/**
 * Lazy capability flags discovered at runtime.
 *
 * Used to avoid hammering the backend with requests we know will 404.
 * Once a chunk-frame fetch returns 404, every later HitCard skips
 * the frame chip entirely (no DOM, no request).
 */

class FeatureFlags {
	/** True when any /api/chunk-frame fetch has returned 404 — implies
        `extract-chunk-frames` hasn't been run yet. */
	framesUnavailable = $state(false);
}

// Module-scope rune state under ssr=true: safe ONLY while every mutation stays browser-gated
// (onMount/$effect/handlers) — the full invariant + the two-session SSR proof live at the
// `graph` singleton in workflow/graph.svelte.ts (#97).
export const features = new FeatureFlags();
