/** The same-origin part of a return path, or `/` when it names another origin.
 *
 *  A return path arrives from a query string or through the issuer, so it is not ours to trust. A
 *  prefix check cannot decide it: the WHATWG URL parser reads `/\evil.com` and `/<TAB>/evil.com` as
 *  `//evil.com`, another host. So the path is parsed against this origin, kept only when the parse
 *  stays here, and answered as the parsed path, search and hash, never as the raw string. */
export function sameOriginPath(candidate: string, origin: string = location.origin): string {
	let url: URL;
	try {
		url = new URL(candidate, origin);
	} catch {
		return '/';
	}
	return url.origin === origin ? `${url.pathname}${url.search}${url.hash}` : '/';
}
