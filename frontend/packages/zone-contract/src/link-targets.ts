/**
 * A domain-relative `<a href>` MUST land on a path some zone actually serves.
 *
 * The sibling gate (`cross-zone-reload`) asks whether a cross-zone link hard-navigates. It never asks
 * whether the target EXISTS — and after the 7 → 4 zone merge that was the question that mattered. The
 * pre-merge zones served `/data`, `/lineage`, `/models` and `/admin` at the root; the merge moved all four
 * under `/lakehouse`, and five links were left pointing at the old paths:
 *
 *   - `home/src/routes/+page.svelte` — the signed-in landing page's PRIMARY call to action, one per project
 *     card, to `/data/projects/<project>`
 *   - `packages/ui/src/lib/shell/project-switcher.svelte` — `/data`, in the shell that EVERY zone mounts,
 *     so it was broken in all four zones at once
 *   - `TableDetail.svelte` ×2 and `PipelineControl.svelte` ×1 — `/lineage`
 *
 * Every one carried `data-sveltekit-reload`, so the reload gate was satisfied: they hard-navigated,
 * correctly and deliberately, to a 404. The request leaves the zone, the ingress hands `/data` to the
 * catch-all (`home`), and home has no such route. 163 green assertions, five dead links.
 *
 * So: resolve each statically-readable domain-relative href against the set of first path segments the
 * estate actually serves — the zone base prefixes, plus whatever the catch-all zone's own route directory
 * contains. `{base}/…` links start with an opaque expression and are same-zone by construction, so they are
 * not the subject here.
 *
 * Deliberately a first-segment check rather than a full route-manifest match. It is the whole of the bug
 * class that the merge created (a segment that no longer exists anywhere), it needs no build output, and it
 * cannot produce a false positive on a dynamic segment deeper in the path.
 */
import { existsSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { EXPR, hrefToPath, walkAnchors } from './cross-zone-reload';
import { FRONTEND_ROOT, svelteBase, zoneDirs } from './manifest';

/** The first path segment of a zone's base, e.g. `/lakehouse` → `lakehouse`. `''` for the catch-all. */
function baseSegment(zone: string): string {
	return svelteBase(zone).replace(/^\//, '').split('/')[0] ?? '';
}

/** The top-level route segments a zone's own `src/routes` serves (`auth`, `capi`, …).
 *
 *  Route groups (`(app)`) are transparent to the URL, and a rest/dynamic first segment would match
 *  anything — both are skipped rather than guessed at, which keeps this a lower bound: the gate can only
 *  ever be too permissive, never wrongly red. */
function routeSegments(zone: string): string[] {
	const dir = resolve(FRONTEND_ROOT, `microfrontends/${zone}/src/routes`);
	if (!existsSync(dir)) return [];
	return readdirSync(dir, { withFileTypes: true })
		.filter((e) => e.isDirectory() && !e.name.startsWith('(') && !e.name.startsWith('['))
		.map((e) => e.name);
}

/** Every first segment the estate serves: the zone prefixes + the catch-all zone's own routes. */
export function servedSegments(): Set<string> {
	const zones = zoneDirs();
	const out = new Set<string>();
	for (const zone of zones) {
		const segment = baseSegment(zone);
		if (segment) out.add(segment);
		// The catch-all owns no prefix, so ITS routes are reachable at the root.
		else for (const s of routeSegments(zone)) out.add(s);
	}
	return out;
}

export interface DeadLink {
	href: string;
	line: number;
	/** The unserved first segment — what makes it dead. */
	segment: string;
}

/** Domain-relative anchors in one component whose first path segment nothing serves. */
export function findDeadLinks(source: string, served: Set<string> = servedSegments()): DeadLink[] {
	const out: DeadLink[] = [];
	for (const { attributes, line } of walkAnchors(source)) {
		const href = attributes.find((a) => a.type === 'Attribute' && a.name === 'href');
		if (!href) continue;
		const path = hrefToPath(href.value);
		// Not domain-relative, or begins with an expression (`{base}/…`, a computed URL) → not ours.
		if (path === null || !path.startsWith('/') || path.startsWith(`/${EXPR}`)) continue;
		const segment = path.slice(1).split(/[/?#]/)[0] ?? '';
		if (segment === '' || segment.includes(EXPR)) continue; // "/" itself, or a dynamic first segment
		if (!served.has(segment)) out.push({ href: path.replaceAll(EXPR, '${…}'), line, segment });
	}
	return out;
}
