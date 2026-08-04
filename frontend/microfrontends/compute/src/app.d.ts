// See https://svelte.dev/docs/kit/types#app.d.ts
import type { AuthLocals } from '@rask/api/bff';

declare global {
	namespace App {
		// The zone's dock reads the session bearer server-side (user-state is OIDC-only at the
		// catalog), so this zone declares the shared auth locals like every other BFF zone.
		interface Locals extends AuthLocals {}
	}
}

export {};
