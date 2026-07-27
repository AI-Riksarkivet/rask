/**
 * Breadcrumb + project derivation for the shared shell — the single source of
 * truth so `app-shell`, `project-switcher`, and any future chrome can't drift.
 *
 * Project-first IA via HOST: the project IS the request host (e.g. demo.localhost),
 * so the pathname carries only the domain + in-domain trail. Every path segment is
 * a crumb (the DOMAIN is the first crumb); the project is the breadcrumb ROOT and is
 * derived from the host, never from the path.
 */

export type Crumb = { id: string; label: string };

/**
 * Crumbs for a domain-relative pathname, in order. Each segment becomes one crumb
 * keyed by its accumulated path prefix (so repeated segments like `/studio/studio`
 * stay unique); the human label drops dashes. The domain (first segment) is kept —
 * it is the first crumb, not the project.
 */
export function pathCrumbs(pathname: string): Crumb[] {
	const segs = pathname.split('/').filter(Boolean);
	return segs.map((seg, i) => ({
		id: segs.slice(0, i + 1).join('/'),
		label: seg.replace(/-/g, ' '),
	}));
}

/**
 * The project label carried by a request host (e.g. `demo.localhost` -> `demo`),
 * or `null` when the host has none. Only a real subdomain is a project label: bare
 * hosts (`localhost`) and IPv4 hosts (`127.0.0.1` — a numeric first label is an
 * octet) have no project.
 */
export function projectFromHost(host: string): string | null {
	const labels = host.split('.');
	if (labels.length > 1 && !/^\d+$/.test(labels[0] ?? '')) {
		return labels[0] || null;
	}
	return null;
}
