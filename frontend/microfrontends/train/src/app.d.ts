// See https://svelte.dev/docs/kit/types#app.d.ts
import type { AuthLocals } from '@rask/api/bff';

declare global {
	namespace App {
		// The zone gained a `+layout.server.ts` that reads `locals.session` / `locals.authEnabled`
		// (the active-project cookie spine), but its `Locals` was still the bare SvelteKit default —
		// so `svelte-check` refused the load signature with "Type 'Locals' is missing the following
		// properties from type 'AuthLocals': session, authEnabled". Every other zone already declares
		// exactly this; `train` and `studio` were simply the two that had never needed it before.
		interface Locals extends AuthLocals {}
	}
}

export {};
