/**
 * The CHROME half of the package, importable WITHOUT the dock: the saved-views store + sidebar and
 * the cross-zone element contract. Exists because the barrel (`.`) also exports `Dock`, so a page
 * that statically imported `ViewSidebar` from it pulled ~100 KB of dockview-core into its ENTRY
 * graph — measured 257 KB on the workbench zone against a 220 ceiling. Invariant 4 (the dock is
 * dynamically imported) requires the sidebar to be importable from a path the dock does not ride.
 */
export { default as ViewSidebar } from './ViewSidebar.svelte';
export { DockViews } from './views.svelte';
export type { ViewSummary, ViewsPhase } from './views.svelte';
