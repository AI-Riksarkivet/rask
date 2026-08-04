// Subject display for access surfaces (#68). FGA subjects carry the OIDC `sub` verbatim —
// for dex that is an opaque base64 blob ("CiQwOGE4…bG9jYWw"), which made the access review
// unreadable: two holders rendered as one run-on nonsense string, and neither was recognizable
// as a person. There is no identity directory to resolve a sub against (dex's static users are
// chart config, not an API), so the honest fix is typographic: keep readable identifiers
// verbatim, ellipsize opaque ones in the middle, and always carry the FULL subject in `title`
// so hover/copy still yields the real value a tuple write needs.

/** A subject is "readable" when a human can recognize it: emails, `role:…#member`, `team:…`,
 *  short names like `root_admin`. Opaque = one long unbroken token (an OIDC sub, a UUID-ish id). */
const OPAQUE_RE = /^[A-Za-z0-9_-]{25,}$/;

export type SubjectDisplay = {
	/** What to render. */
	label: string;
	/** The full subject — always set, so every rendering can carry it as a tooltip. */
	title: string;
};

/** Ellipsize the middle of one opaque token: `CiQwOGE4Njg0…bG9jYWw`. Callers gate on OPAQUE_RE
 *  first, which already requires ≥25 characters — a second length threshold here would only be a
 *  number a reader has to reconcile with the regex. */
function shorten(token: string): string {
	return `${token.slice(0, 12)}…${token.slice(-7)}`;
}

/** Display form of one FGA subject. `user:<opaque-sub>` keeps its type prefix; a bare opaque
 *  token is shortened alone; anything readable passes through untouched. */
export function subjectDisplay(subject: string): SubjectDisplay {
	const sep = subject.indexOf(':');
	if (sep > 0) {
		const type = subject.slice(0, sep);
		const rest = subject.slice(sep + 1);
		if (OPAQUE_RE.test(rest)) return { label: `${type}:${shorten(rest)}`, title: subject };
		return { label: subject, title: subject };
	}
	if (OPAQUE_RE.test(subject)) return { label: shorten(subject), title: subject };
	return { label: subject, title: subject };
}
