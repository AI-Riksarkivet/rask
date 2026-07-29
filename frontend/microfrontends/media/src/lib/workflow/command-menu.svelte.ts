/** Open/closed state of the workflow ⌘K command palette — its own tiny runes
 *  module so the toolbar button and the palette component (mounted at page
 *  level) share it without prop-drilling through the canvas. */
class CommandMenuState {
	open = $state(false);

	toggle(): void {
		this.open = !this.open;
	}
}

export const commandMenu = new CommandMenuState();
