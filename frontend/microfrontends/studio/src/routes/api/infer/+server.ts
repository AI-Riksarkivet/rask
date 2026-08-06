/**
 * The flows sandbox's inference proxy — POST raw payload bytes to a live Ray
 * Serve app. Bytes ride a `+server.ts`, never a remote function (the estate's
 * transport ruling), and the gateway's `/api/serve` row is read-only by design,
 * so the zone reaches Serve's own HTTP ingress (`:8000`) directly — the same
 * lane as the models zone's `/api/infer`, through the same shared @rask/api
 * helper so the two proxies cannot drift.
 *
 * `?app=<slug>` picks the Serve app (the Model node fills it from live
 * discovery); the slug is validated here so this stays an inference door, not
 * a generic proxy into the Serve origin.
 */
import type { RequestHandler } from './$types';
import { json } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { proxyServeInfer } from '@rask/api/serve-proxy';

/** Serve's HTTP ingress ORIGIN (no app path — `?app=` supplies it). In-cluster
 *  this points at the Ray head's serve port; unset means local `make serve-up-htrflow`. */
const SERVE_ORIGIN = env.STUDIO_SERVE_URL ?? 'http://localhost:8000';

/** One path segment, lowercase slug — matches how deploy_serve.py names apps. */
const APP_RE = /^[a-z0-9][a-z0-9_-]*$/;

/** The path WITHIN the app. A Serve `route_prefix` reaches only the app root, and every
 *  OpenAI-compatible deployment on the cluster puts its endpoints under `/v1/...` — so without a
 *  path, that whole class of app is uncallable (measured: POST /gemma-31b 404s,
 *  POST /gemma-31b/v1/chat/completions answers). Constrained rather than free-form: slashed segments
 *  only, no `..`, no scheme and no authority, so this cannot become an open proxy into the cluster —
 *  the same reason `app` is a slug and not a URL. */
const PATH_RE = /^(\/[A-Za-z0-9._~-]+)*\/?$/;

export const POST: RequestHandler = (event) => {
	const app = event.url.searchParams.get('app') ?? 'htrflow';
	if (!APP_RE.test(app)) {
		return Promise.resolve(
			json({ reason: 'bad_app', detail: `Not a Serve app name: ${app}` }, { status: 400 }),
		);
	}
	const path = event.url.searchParams.get('path') ?? '';
	if (path !== '' && !PATH_RE.test(path)) {
		return Promise.resolve(
			json({ reason: 'bad_path', detail: `Not a Serve app path: ${path}` }, { status: 400 }),
		);
	}
	return proxyServeInfer(event, { target: `${SERVE_ORIGIN}/${app}${path}` });
};
