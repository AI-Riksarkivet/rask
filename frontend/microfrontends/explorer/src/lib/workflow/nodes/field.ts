/** Shared base class for the native form controls inside workflow nodes.
 *  `nodrag` stops Svelte Flow from treating an input drag as a node drag.
 *  Width is left to the caller (`w-full`, `w-16`, …). */
export const FIELD_CLASS =
	'nodrag rounded border border-border bg-background px-2 py-1 text-xs text-foreground outline-none focus:border-primary';
