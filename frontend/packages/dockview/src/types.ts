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
 * What a registry entry will accept as a body — either shape.
 *
 * Every panel in this repo today declares NO props: all nine read their data from context or from a
 * remote `query()`, and simply ignore what the renderer hands them. That is legitimate, but it does
 * not type-check against {@link PanelComponent} alone, because `svelte2tsx` compiles a props-less
 * component to `Component<Record<string, never>>` — a type that REJECTS extra props — and component
 * props are compared contravariantly.
 *
 * It compiled before this type existed only because the zones left `const panels = {…}` unannotated
 * and the check happened at the `<Dock>` prop boundary, where Svelte's generated typings are
 * bivariant. That is not a safety net worth relying on: with the annotation removed, an entry missing
 * its required `label` also type-checked clean, which is exactly the mistake the required field exists
 * to prevent. So the union is stated here and the zones annotate.
 */
export type AnyPanelComponent = PanelComponent<never> | Component<Record<string, never>>;

/**
 * The glyph a picker row shows.
 *
 * Declared STRUCTURALLY rather than imported from `@lucide/svelte`, the same stance `@rask/ui` takes
 * with its client props: this package must not acquire an icon-library dependency merely to describe
 * one. A lucide icon satisfies it because every member of `LucideProps` is optional.
 *
 * The zone passes the COMPONENT, never a name string — a name would force this package to import
 * lucide's barrel to resolve it, defeating tree-shaking over ~1500 icons for the sake of nine.
 */
export type PanelIcon = Component<{ size?: number | string; class?: string }>;

/**
 * One entry in the zone's panel catalogue: what to render, and what to call it.
 *
 * `label` is REQUIRED. The add-panel picker lists these, and the only fallback for a missing label is
 * the registry key — a lowercase identifier where a human-readable name belongs. Required makes that
 * state unrepresentable rather than merely discouraged.
 *
 * `label` is the DEFAULT tab title at add time, not a live binding: dockview writes `title` into the
 * saved layout, so renaming a label here does not rename panels already in a stored workspace.
 */
export interface PanelEntry {
	/** The body — a component declaring `PanelProps`, or none at all. See {@link AnyPanelComponent}. */
	readonly component: AnyPanelComponent;
	/** Human-readable name. Shown in the picker, and used as the new panel's title. */
	readonly label: string;
	/** Optional glyph for the picker row. */
	readonly icon?: PanelIcon;
	/**
	 * Extra search terms. The picker matches over label + key + these, so someone typing "dag",
	 * "provenance" or "lineage" finds the panel labelled "Graph".
	 */
	readonly keywords?: readonly string[];
}

/**
 * The zone's panel catalogue, keyed by the `component` string it passes to `api.addPanel(...)`.
 * dockview resolves a serialized layout by that same string, so these keys are the stable contract
 * between a persisted layout and the code — renaming one orphans every saved layout that used it.
 * The VALUE shape is free to grow; the keys are not.
 *
 * This was `Record<string, PanelComponent<never>>`. Widening it is a BREAKING change to the value, and
 * a deliberate one: a union that still accepted a bare component would have to fall back to the
 * registry key for a label — rendering `topics` where "Topic results" belongs — which institutionalises
 * the half-migration it exists to avoid, and forces `typeof entry === 'function'` narrowing at every
 * read site forever (Svelte 5 components ARE functions). The package is private with three in-repo
 * consumers, so the migration is atomic and was done atomically.
 */
export type PanelRegistry = Record<string, PanelEntry>;

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
