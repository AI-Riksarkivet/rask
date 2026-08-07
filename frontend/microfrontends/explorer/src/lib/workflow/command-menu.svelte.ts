/** Open/closed state of the workflow ⌘K command palette — its own tiny runes
 *  module so the toolbar button and the palette component (mounted at page
 *  level) share it without prop-drilling through the canvas. */
class CommandMenuState {
	open = $state(false);

	toggle(): void {
		this.open = !this.open;
	}
}

// Module-scope rune state under ssr=true: safe ONLY while every mutation stays browser-gated
// (onMount/$effect/handlers) — the full invariant + the two-session SSR proof live at the
// `graph` singleton in workflow/graph.svelte.ts (#97).
export const commandMenu = new CommandMenuState();
