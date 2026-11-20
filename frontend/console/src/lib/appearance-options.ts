import type { AppearanceOptions } from '@riksarkivet/ui/appearance';

// The console's appearance, read by two callers that must agree: the Vite plugin bakes it into the boot
// script that paints the kept (or default) theme and mode before the app runs, and the root layout starts
// the store with it. A first visit is dark; a person's choice is kept in their browser under the key.
export const APPEARANCE: AppearanceOptions = {
	storageKey: 'rask-console-appearance',
	defaults: { theme: 'ink', mode: 'dark' },
};
