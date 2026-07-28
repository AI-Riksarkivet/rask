/**
 * The seams a consuming zone injects. Nothing here knows what a panel renders, which zone mounts it,
 * or where a layout is stored — that is the whole point. `@rask/dockview` is estate infrastructure
 * like `@rask/ui`, so every zone-shaped decision arrives as a prop.
 *
 * This module deliberately re-exports NOTHING from `dockview`. A consumer holds the real
 * `DockviewApi` and calls its documented methods, so it imports those types from `dockview` itself —
 * one API to learn, not two, and no wrapper to drift out of date.
 *
 * The dependency is `dockview`, NOT `dockview-core`. Their type entrypoints are identical
 * (`export * from 'dockview-core'`), which makes core look like the honest, leaner choice — it is
 * not. `dockview`'s RUNTIME entry is a 37 KB registration layer that calls `markDockviewPackageLoaded()`
 * and `registerModules(...)` for ContextMenu, KeyboardDocking, AdvancedDnD, TabGroupChips and
 * Accessibility. Import core directly and all five are silently absent — including the aria-live
 * announcements — and the library logs `do not use "dockview-core" directly` once, at runtime, where
 * nothing fails.
 */
import type { Component } from 'svelte';
import type { DockviewApi, DockviewPanelApi, Parameters, SerializedDockview } from 'dockview';

/**
 * What every panel component receives.
 *
 * `params` is a **stable, deeply-reactive object whose identity never changes**. dockview delivers
 * parameter updates through `api.onDidParametersChange`, and the renderer syncs them by MUTATING
 * this object in place rather than replacing it — a replacement would not reach a component that
 * already captured the old reference at mount time. Read `params.foo` in a `$derived` and it
 * tracks; destructure it and it does not, exactly like `$props()`.
 */
export interface PanelProps<P extends object = Parameters> {
	/** The panel's parameters. Mutated in place; never reassigned. */
	params: P;
	/** This panel's own api — `setTitle`, `close`, `moveTo`, `onDidVisibilityChange`, … */
	api: DockviewPanelApi;
	/** The whole dock's api, for a panel that needs to open or address its siblings. */
	containerApi: DockviewApi;
}

/** A Svelte component usable as a panel body. */
export type PanelComponent<P extends object = Parameters> = Component<PanelProps<P>>;

/**
 * The zone's panel catalogue, keyed by the `component` string it passes to `api.addPanel(...)`.
 * dockview resolves a serialized layout by that same string, so these keys are the stable contract
 * between a persisted layout and the code — renaming one orphans every saved layout that used it.
 */
export type PanelRegistry = Record<string, PanelComponent<never>>;

/**
 * The three outcomes of reading a stored layout, and the reason the middle one cannot be collapsed
 * into the others.
 *
 * `absent` means the user genuinely has no saved layout — seed a default and saving is safe.
 * `unreadable` means one EXISTS and could not be read (schema drift, an owner mismatch, or the
 * store being unreachable). Reported as `absent`, the dock would seed a default layout and the next
 * autosave would overwrite a workspace that is still there. This is the same contract the media
 * zone's `$lib/user-state.ts` already implements against the catalog's 409, and the same reason.
 */
export type LayoutRead =
	| { readonly status: 'ok'; readonly layout: SerializedDockview }
	| { readonly status: 'absent' }
	| { readonly status: 'unreadable'; readonly detail: string };

/**
 * Where a layout lives. Injected, so the package neither knows nor cares whether that is the
 * catalog's per-subject user-state document on the Dapr state store, a zone-local mirror, or
 * nothing at all.
 */
export interface LayoutStore {
	/** Read the caller's layout. Must distinguish absent from unreadable — see {@link LayoutRead}. */
	load(): Promise<LayoutRead>;
	/** Persist a layout. Returns whether the STORE accepted it; a local mirror write is not a save. */
	save(layout: SerializedDockview): Promise<boolean>;
}
