import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// Same-origin proxy for ANNOTATION IMPORT — Arrow IPC bytes to the annotator service's
// `POST /tasks/{id}/import`.
//
// A `+server.ts` route and not a remote function, deliberately, and this is the estate's standing
// rule rather than a preference here: devalue carries an ArrayBuffer as base64 INSIDE the payload
// string, which costs +33% on the wire, triple-buffers the whole thing (bytes -> base64 -> bytes,
// no streaming) and throws away content-type. An import file is exactly the payload kind that rule
// exists for. Every JSON value surface beside this one — the draft save, the task events — stays on
// `.remote.ts`.
//
// `requireSession: true` for the same reason the draft save has it: an import is a write, and a
// write must be attributable. Imported rows are stamped with the verified subject as author by the
// service, so an anonymous import would put unattributable annotations into someone's draft.
const ANNOTATOR_API = env.ANNOTATOR_PROJECTS_API ?? env.ANNOTATOR_API ?? 'http://localhost:8103';

export const POST = makeBackendProxy({
	backendUrl: ANNOTATOR_API,
	// The service mounts its task routes at `/tasks`, not under `/api` — so unlike this zone's
	// annotations proxy (which uses KEEP_API_PREFIX) the `/api` really is stripped here.
	stripPrefix: /^\/api/,
	requireSession: true,
	// The refusals are the whole point of the endpoint: a foreign label, a dangling relation, a
	// duplicate id all come back as a 400 naming the offender, and the UI shows those words.
	forwardResponseHeaders: true,
});
