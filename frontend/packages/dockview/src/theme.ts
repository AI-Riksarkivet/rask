/**
 * rask's dockview theme — the NON-CSS half.
 *
 * A `DockviewTheme` is two things at once: a `className` that selects a block of `--dv-*` custom
 * properties (that half lives in `styles.css`, driven by the OKLCH tokens), and a handful of fields
 * that are *behavioural* and cannot be expressed in CSS at all. Only the second half belongs here.
 *
 * `colorScheme` is deliberately UNSET. It is a static field on a theme object, but rask flips theme
 * at runtime by toggling `.dark` on `<html>` (mode-watcher), and every `--dv-*` below resolves
 * through a rask token that is itself re-declared under `.dark`. So the dock re-themes on a mode
 * flip with no JavaScript and no `api.updateOptions` round trip. Setting `colorScheme` would add a
 * second source of truth that could only ever be stale.
 */
import type { DockviewTheme } from 'dockview';

export const raskDockTheme: DockviewTheme = {
	name: 'rask',
	className: 'dockview-theme-rask',
	/** A hairline gutter between groups; the sash draws inside it. */
	gap: 2,
	/** Mount the drag overlay on the dock root so it is never clipped by a group's own overflow. */
	dndOverlayMounting: 'absolute',
	/** Highlight the whole group including its tab strip — the drop target IS the group. */
	dndPanelOverlay: 'group',
	/** A thin insertion strip reads correctly against a bordered, spaced theme; 'fill' does not. */
	dndTabIndicator: 'line',
	/** Chrome-style underline that wraps the active tab, matching the estate's nav treatment. */
	tabGroupIndicator: 'wrap',
	/** Tabs slide apart to reveal the insertion gap instead of snapping. */
	tabAnimation: 'smooth',
};
