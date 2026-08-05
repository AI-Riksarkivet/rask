// See https://svelte.dev/docs/kit/types#app.d.ts
import type { AuthLocals } from '@rask/api/bff';

declare global {
	namespace App {
		// The flows BFF (`api/infer`) gates on the session server-side, so this
		// zone declares the shared auth locals like every other BFF zone.
		interface Locals extends AuthLocals {}
	}
}

export {};
