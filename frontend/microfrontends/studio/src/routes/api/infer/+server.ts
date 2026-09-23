/**
 * The flows sandbox's inference proxy — POST raw payload bytes to a live Ray
 * Serve app. Bytes ride a `+server.ts`, never a remote function (the estate's
 * transport ruling), and the gateway's `/api/serve` row is read-only by design,
 * so the zone reaches Serve's own HTTP ingress directly, through the shared
 * @rask/api helper that owns the pass-through contract and the failure taxonomy.
 *
 * `?app=<slug>` names the Serve app (the Model node fills it from live
 * discovery) and is REQUIRED: the door has no default app, so a call that does
 * not say which app it wants is refused rather than routed somewhere it never
 * asked to go. Both `app` and `path` are validated here so this stays an
 * inference door, not a generic proxy into the Serve origin.
 * Pinned by `src/lib/flows/infer-route.test.ts`.
 */
import type { RequestHandler } from './$types';
import { json } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { proxyServeInfer } from '@rask/api/serve-proxy';

/** Serve's HTTP ingress ORIGIN (no app path — `?app=` supplies it). In-cluster
 *  this points at the Ray head's serve port; unset means a local Ray Serve on its
 *  default HTTP port. */
const SERVE_ORIGIN = env.STUDIO_SERVE_URL ?? 'http://localhost:8000';

/** One path segment, lowercase slug — the shape of a Serve application's route prefix. */
const APP_RE = /^[a-z0-9][a-z0-9_-]*$/;

/** The path WITHIN the app. A Serve `route_prefix` reaches only the app root, and every
 *  OpenAI-compatible deployment on the cluster puts its endpoints under `/v1/...` — so without a
 *  path, that whole class of app is uncallable (measured: POST /gemma-31b 404s,
 *  POST /gemma-31b/v1/chat/completions answers). Constrained rather than free-form: slashed segments
 *  only, no scheme and no authority, so this cannot become an open proxy into the cluster — the same
 *  reason `app` is a slug and not a URL. */
const PATH_RE = /^(\/[A-Za-z0-9._~-]+)*\/?$/;

/** A segment made only of dots. URL parsing resolves `.` and `..` segments, so `/<app>/../x` would
 *  leave the validated app and land on another app (or on Serve's own `/-/` routes) — the slug check
 *  on `app` means nothing unless the path cannot climb out of it. */
const DOT_SEGMENT_RE = /(^|\/)\.+(\/|$)/;

export const POST: RequestHandler = (event) => {
	const app = event.url.searchParams.get('app') ?? '';
	if (!APP_RE.test(app)) {
		return Promise.resolve(
			json(
				{
					reason: 'bad_app',
					detail: app ? `Not a Serve app name: ${app}` : 'Name the Serve app to call (?app=).',
				},
				{ status: 400 },
			),
		);
	}
	const path = event.url.searchParams.get('path') ?? '';
	if (path !== '' && (!PATH_RE.test(path) || DOT_SEGMENT_RE.test(path))) {
		return Promise.resolve(
			json({ reason: 'bad_path', detail: `Not a Serve app path: ${path}` }, { status: 400 }),
		);
	}
	return proxyServeInfer(event, { target: `${SERVE_ORIGIN}/${app}${path}` });
};
