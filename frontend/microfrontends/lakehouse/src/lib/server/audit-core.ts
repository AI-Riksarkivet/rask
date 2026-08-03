import { env } from '$env/dynamic/private';

// #77 the audit-trail read, ONE implementation for its two doors: the zone UI's remote function
// (`admin/remote/audit.remote.ts`) and the thin `/api/audit` route kept alive for the workbench
// zone's <rask-lakehouse-audit> element (a custom-element bundle cannot import a .remote.ts — the
// cross-zone blocker open_transport.md records). Everything here moved VERBATIM from the route:
// the estate-admin gate, the allow-listed time windows, the proven SQL shape, the log_attributes
// flattening (the 2026-07-21 "every field renders —" bug), and the post-filters.

const GREPTIME_API = env.GREPTIME_API ?? '';
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

export type AuditEvent = {
	timestamp: string;
	action: string;
	outcome: string;
	subject: string;
	resource: string;
};

export type AuditParams = {
	since?: string;
	action?: string;
	outcome?: string;
	subject?: string;
	resource?: string;
};

export type AuditResult =
	| { ok: true; events: AuditEvent[] }
	| { ok: false; status: number; detail: string };

type SessionShape = { accessToken: string } | null | undefined;

// Returns null when the caller is an estate admin; otherwise the {status, detail} to answer with. The
// probe is a side-effect-free `GET /v1/events` with a past-any-head cursor: an estate admin gets an
// empty page (an empty poll is never audited, so the probe leaves no trail noise), anyone else the
// catalog's own 401/403 verdict. Bearer-FORWARDED (never a service token); fail closed.
async function estateAdminGate(
	fetchFn: typeof fetch,
	accessToken: string,
): Promise<{ status: number; detail: string } | null> {
	try {
		const res = await fetchFn(`${CATALOG_API}/v1/events?since=${Number.MAX_SAFE_INTEGER}`, {
			headers: { authorization: `Bearer ${accessToken}` },
			signal: AbortSignal.timeout(5000),
		});
		if (res.ok) return null;
		if (res.status === 403) return { status: 403, detail: 'the audit trail is estate-admin only' };
		if (res.status === 401) return { status: 401, detail: 'sign in to view the audit trail' };
		return { status: 503, detail: 'audit admin authorization is unavailable' };
	} catch (err) {
		console.error(`audit admin-gate upstream failure: ${String(err)}`);
		return { status: 503, detail: 'audit admin authorization is unavailable' };
	}
}

function pick(row: Record<string, unknown>, ...keys: string[]): string {
	for (const k of keys) {
		const v = row[k];
		if (v !== undefined && v !== null && v !== '') return String(v);
	}
	return '';
}

// GreptimeDB's OTLP logs table nests attributes in a `log_attributes` JSON column (keys
// `audit.action`, `audit.outcome`, …) — NOT flat top-level columns. Merge that JSON up into the row
// lookup so `pick("audit.action")` finds it; else every field renders "—" (2026-07-21, which the
// flat-column test mock had hidden — the reworked spec seeds the NESTED shape on purpose).
function flattenRow(o: Record<string, unknown>): Record<string, unknown> {
	const merged: Record<string, unknown> = {};
	for (const col of ['resource_attributes', 'scope_attributes', 'log_attributes']) {
		const raw = o[col];
		let obj: unknown = raw;
		if (typeof raw === 'string') {
			try {
				obj = JSON.parse(raw);
			} catch {
				obj = null;
			}
		}
		if (obj && typeof obj === 'object') Object.assign(merged, obj);
	}
	return { ...o, ...merged };
}

type SqlResponse = {
	output?: { records?: { schema?: { column_schemas?: { name: string }[] }; rows?: unknown[][] } }[];
};

/** Read the trail. `authEnabled`/`session` come from the caller's locals; both doors pass their own. */
export async function readAuditTrail(
	fetchFn: typeof fetch,
	locals: { authEnabled: boolean; session: SessionShape },
	params: AuditParams,
): Promise<AuditResult> {
	if (locals.authEnabled && !locals.session) {
		return { ok: false, status: 401, detail: 'sign in to view the audit trail' };
	}
	// Authorization (not just authentication): a governed, signed-in caller must be an ESTATE admin —
	// else any project's admin could read the whole estate's cross-tenant trail.
	if (locals.authEnabled && locals.session) {
		const denied = await estateAdminGate(fetchFn, locals.session.accessToken);
		if (denied) return { ok: false, status: denied.status, detail: denied.detail };
	}
	if (!GREPTIME_API) {
		return {
			ok: false,
			status: 501,
			detail: 'the audit viewer requires the observability stack (GreptimeDB)',
		};
	}
	// Proven query shape (docs/KIND-RUNBOOK.md): filter to audit records, newest first, bounded. The
	// finer filters are applied below over the returned columns. The TIME window belongs in SQL and is
	// an ALLOW-LIST, never interpolated user text — an unknown value falls back to 24h.
	const WINDOWS: Record<string, string> = {
		'1h': "timestamp > now() - INTERVAL '1 hour'",
		'24h': "timestamp > now() - INTERVAL '1 day'",
		'7d': "timestamp > now() - INTERVAL '7 days'",
		'30d': "timestamp > now() - INTERVAL '30 days'",
		all: '',
	};
	const clause = WINDOWS[params.since ?? '24h'] ?? WINDOWS['24h']!;
	const sql =
		`SELECT * FROM opentelemetry_logs WHERE body = 'audit'${clause ? ` AND ${clause}` : ''}` +
		' ORDER BY timestamp DESC LIMIT 500';
	try {
		const res = await fetchFn(`${GREPTIME_API}/v1/sql?db=public`, {
			method: 'POST',
			headers: {
				'content-type': 'application/x-www-form-urlencoded',
				// Greptime here is the unauthenticated in-cluster endpoint; the bearer rides along only so
				// the hermetic mock can key its seeds per test (same rationale as the jetstream monitor).
				...(locals.session ? { authorization: `Bearer ${locals.session.accessToken}` } : {}),
			},
			body: `sql=${encodeURIComponent(sql)}`,
		});
		if (!res.ok) {
			return { ok: false, status: 502, detail: `greptime ${res.status}` };
		}
		const body = (await res.json()) as SqlResponse;
		const records = body.output?.[0]?.records;
		const cols = (records?.schema?.column_schemas ?? []).map((c) => c.name);
		const rows = records?.rows ?? [];
		let events: AuditEvent[] = rows.map((r) => {
			const raw: Record<string, unknown> = {};
			cols.forEach((c, i) => (raw[c] = r[i]));
			const o = flattenRow(raw); // lift `log_attributes` JSON so the audit.* keys are reachable
			return {
				timestamp: pick(o, 'timestamp', 'time'),
				action: pick(o, 'audit.action', 'action'),
				outcome: pick(o, 'audit.outcome', 'outcome'),
				subject: pick(o, 'audit.subject', 'subject'),
				resource: pick(o, 'audit.resource', 'resource'),
			};
		});
		// Post-filter over the parsed events (case-insensitive substring for subject/resource, exact
		// for the low-cardinality action/outcome), so the query shape stays fixed + proven.
		const norm = (v: string | undefined) => v?.trim().toLowerCase() ?? '';
		const [action, outcome, subject, resource] = [
			norm(params.action),
			norm(params.outcome),
			norm(params.subject),
			norm(params.resource),
		];
		if (action) events = events.filter((e) => e.action.toLowerCase() === action);
		if (outcome) events = events.filter((e) => e.outcome.toLowerCase() === outcome);
		if (subject) events = events.filter((e) => e.subject.toLowerCase().includes(subject));
		if (resource) events = events.filter((e) => e.resource.toLowerCase().includes(resource));
		return { ok: true, events };
	} catch (err) {
		console.error(`audit upstream failure: ${String(err)}`);
		return { ok: false, status: 502, detail: String(err) };
	}
}
