// The console's browser transport, re-exported by name so call sites keep importing `$lib/http`:
// same-origin `/api/*` calls with the signed-in person's bearer (`@rask/api/client`).
export {
	type ApiResult,
	getJSON,
	requestBinary,
	requestBytes,
	requestJSON,
} from '@rask/api/client';
