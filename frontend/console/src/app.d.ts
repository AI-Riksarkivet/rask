// See https://svelte.dev/docs/kit/types#app.d.ts
declare global {
	namespace App {
		interface Error {
			message: string;
			/** The error page's heading and `<title>`, when the code that refused names what was
			 *  refused ("You don't have access to Settings"); absent, the page words the status. */
			title?: string;
		}
	}
}

export {};
