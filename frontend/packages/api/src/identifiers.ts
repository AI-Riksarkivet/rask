/**
 * The catalog's identifier grammar — the ONE place the estate agrees on how a hierarchical catalog
 * object is written as a flat string.
 *
 * The catalog is a hierarchy (warehouse → namespace → nested namespaces → table), but the two things
 * that consume an object's identity are flat strings: an HTTP path segment, and an OpenFGA object id.
 * So the hierarchy is joined with a delimiter — `bronze$pages` is the table `pages` inside the
 * namespace `bronze`, and `acme$bronze$pages` is the same table one level deeper.
 *
 * This lives in `@rask/api` rather than a zone's `$lib` because zones do not share `$lib` and more than
 * one zone splits these ids. It previously lived in the lakehouse alone, so the `home` zone carried its
 * own copy — and that copy was WRONG (see `parentNamespace`). A grammar that belongs to the catalog
 * contract belongs beside the catalog client, not inside whichever zone happened to need it first.
 *
 * ## Why the delimiter is a constant and not fetched
 *
 * `LANCE_NS_DELIMITER` looks operator-settable, and I described it that way before checking. It is not:
 * `chart/templates/services.yaml` sets `{ name: LANCE_NS_DELIMITER, value: "$" }` as a LITERAL and
 * exposes no `values.yaml` key for it, so changing the delimiter is already an edit to a file in this
 * repo. A constant in the same repo therefore ships in the same commit as the chart change, and a
 * config endpoint to discover the value at runtime would buy nothing. That is a decision, not a
 * deferral — do not add `GET /v1/config` for this.
 */

/** The character joining hierarchy levels into one flat id. Mirrors the catalog's `settings.delimiter`. */
export const DELIMITER = '$';

/**
 * The namespace containing a table: EVERY segment but the last. A bare name is its own root.
 *
 * `lastIndexOf`, and the difference from `indexOf` is a correctness bug rather than a nicety. The
 * backend is the authority and states it in one line — `parent_namespace_id` is "all segments but the
 * last" (`packages/service-kit/src/service_kit/governed/fga.py:187-201`) — and THAT is the id the grant
 * and check paths key on. Splitting on the first delimiter instead names a different object:
 *
 *     acme$bronze$pages   lastIndexOf → acme$bronze   ← the real parent; what authz checks
 *                         indexOf     → acme          ← a different object, possibly nonexistent
 *
 * A frontend deriving the second one does not render oddly, it asks authorization about the wrong
 * object and believes the answer.
 */
export function parentNamespace(id: string): string {
	return id.includes(DELIMITER) ? id.slice(0, id.lastIndexOf(DELIMITER)) : id;
}

/**
 * The last segment of a nested id — the table's own name, or a namespace's own name without its
 * ancestors. Used for display, and for reading a property that belongs to the object itself rather
 * than to anything above it (the medallion stage is one: `acme$acme-silver` is `silver`, because the
 * ancestors above the leaf are tenancy, not tier).
 */
export function leafSegment(id: string): string {
	return id.includes(DELIMITER) ? id.slice(id.lastIndexOf(DELIMITER) + 1) : id;
}

/** The `<namespace>$` prefix of a table id, or `''` for a root-level table. Handy for placeholders. */
export function namespacePrefix(id: string): string {
	return id.includes(DELIMITER) ? id.slice(0, id.lastIndexOf(DELIMITER) + 1) : '';
}
