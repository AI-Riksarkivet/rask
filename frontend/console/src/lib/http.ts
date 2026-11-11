// The console's browser transport, re-exported by name so call sites keep importing `#lib/http`:
// same-origin `/api/*` calls with the signed-in person's bearer (`#lib/api/client.ts`).
export {
	type ApiResult,
	getJSON,
	requestBinary,
	requestBytes,
	requestJSON,
} from '#lib/api/client.ts';
