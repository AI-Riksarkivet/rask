// The catalog's label doors (ADR 0211), in memory, for the two mock catalogs (`mock-catalog.ts` and
// `admin/mock-catalog.ts`): a project's definitions, the labels a table or a namespace carries, and a
// project's labelled objects. Like everything mutable in those mocks it is kept per BEARER, so the
// fullyParallel suite shares no state.
//
// It answers as the catalog does where a spec can tell: a name outside the label grammar or a reserved
// one is 400 (code 13), an evolution that narrows the scope or the allowed values or changes the kind
// is 400 (code 13), a value that does not fit the definition is 400 (code 13), and a delete while an
// object carries the label is 409 (code 3). Who may do what is the catalog's FGA and is not modelled:
// a spec that means a refusal seeds the door's answer, which the mocks serve first.
//
// `POST /__mock/labels` seeds a bearer's world: `{bearer, definitions?, objects?, warehouses?}`, each
// object `{kind, id, project, labels}`, and `warehouses` each warehouse's project, which names the
// project of an object that carries no label yet (a describe answers its project's `available`).

type Kind = 'marker' | 'free_text' | 'enumerated';
type Scope = 'namespace' | 'table';

type Definition = {
	uuid: string;
	project: string;
	name: string;
	description?: string | null;
	scope: Scope[];
	kind: Kind;
	allowed_values?: string[] | null;
	defined_by?: string | null;
	defined_at: string;
};

type Labelled = {
	kind: Scope;
	id: string;
	uuid: string;
	project: string;
	labels: Record<string, string | null>;
};

type World = {
	definitions: Definition[];
	objects: Map<string, Labelled>;
	warehouses: Map<string, string>;
};

const NAME = /^[a-z0-9][a-z0-9_-]{0,62}(\.[a-z0-9][a-z0-9_-]{0,62}){0,3}$/;
const DEFINED_AT = '2026-11-15T12:00:00Z';

const worlds = new Map<string, World>();
const worldOf = (bearer: string): World => {
	let world = worlds.get(bearer);
	if (!world) {
		world = { definitions: [], objects: new Map(), warehouses: new Map() };
		worlds.set(bearer, world);
	}
	return world;
};

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

const problem = (status: number, code: number, detail: string): Response =>
	json({ type: 'about:blank', title: 'LabelError', status, code, detail }, status);

const objectKey = (kind: Scope, id: string): string => `${kind}:${id}`;

const answer = (o: Labelled): Record<string, unknown> => ({
	kind: o.kind,
	id: o.id,
	uuid: o.uuid,
	labels: o.labels,
});

/** The world's record of an object, or a fresh one: an object that carries no label has one too, in
 *  the project of the warehouse its id opens with. */
function objectOf(world: World, kind: Scope, id: string): Labelled {
	return (
		world.objects.get(objectKey(kind, id)) ?? {
			kind,
			id,
			uuid: `uuid-${kind}-${id}`,
			project: world.warehouses.get(id.split('$')[0] ?? '') ?? '',
			labels: {},
		}
	);
}

/** A describe's answer: the object's labels and the definitions of its project for its kind. */
const described = (world: World, o: Labelled): Record<string, unknown> => ({
	...answer(o),
	available: world.definitions.filter((d) => d.project === o.project && d.scope.includes(o.kind)),
});

async function body(req: Request): Promise<Record<string, unknown>> {
	return ((await req.json().catch(() => ({}))) ?? {}) as Record<string, unknown>;
}

function define(world: World, project: string, b: Record<string, unknown>): Response {
	const name = typeof b.name === 'string' ? b.name : '';
	if (!NAME.test(name)) return problem(400, 13, `label name '${name}' is not a label name`);
	if (name === 'rask' || name.startsWith('rask.'))
		return problem(400, 13, `label name '${name}' is reserved`);
	const scope = (Array.isArray(b.scope) ? b.scope : []) as Scope[];
	if (scope.length === 0)
		return problem(400, 13, 'a label applies to a namespace, a table or both');
	const kind = b.kind as Kind;
	const allowed = (Array.isArray(b.allowed_values) ? b.allowed_values : null) as string[] | null;
	if (kind === 'enumerated' && (allowed ?? []).length === 0)
		return problem(400, 13, 'an enumerated label lists its allowed values');
	const held = world.definitions.find((d) => d.project === project && d.name === name);
	if (held) {
		if (held.kind !== kind) return problem(400, 13, `label '${name}' is a ${held.kind}`);
		if (held.scope.some((s) => !scope.includes(s)))
			return problem(400, 13, `label '${name}' may only apply to more`);
		if ((held.allowed_values ?? []).some((v) => !(allowed ?? []).includes(v)))
			return problem(400, 13, `label '${name}' may only allow more values`);
	}
	const definition: Definition = {
		uuid: held?.uuid ?? `uuid-label-${project}-${name}`,
		project,
		name,
		description: typeof b.description === 'string' ? b.description : null,
		scope,
		kind,
		allowed_values: kind === 'enumerated' ? allowed : null,
		defined_by: 'user:e2e',
		defined_at: DEFINED_AT,
	};
	world.definitions = [...world.definitions.filter((d) => d !== held), definition];
	return json(definition);
}

function setLabel(world: World, kind: Scope, id: string, b: Record<string, unknown>): Response {
	const name = typeof b.name === 'string' ? b.name : '';
	const value = typeof b.value === 'string' ? b.value : null;
	const held = objectOf(world, kind, id);
	// The object's project is the definition's: the mock keeps no warehouse registry.
	const definition = world.definitions.find(
		(d) => d.name === name && (held.project === '' || d.project === held.project),
	);
	if (!definition) return problem(400, 13, `no label '${name}' is defined for this ${kind}`);
	if (!definition.scope.includes(kind))
		return problem(400, 13, `label '${name}' does not apply to a ${kind}`);
	if (definition.kind === 'marker' && value !== null)
		return problem(400, 13, `label '${name}' is a marker and carries no value`);
	if (definition.kind === 'free_text' && (value === null || value.length < 1 || value.length > 256))
		return problem(400, 13, `label '${name}' carries a text of 1 to 256 characters`);
	if (definition.kind === 'enumerated' && !(definition.allowed_values ?? []).includes(value ?? ''))
		return problem(400, 13, `label '${name}' carries one of its allowed values`);
	const next: Labelled = {
		...held,
		project: definition.project,
		labels: { ...held.labels, [name]: value },
	};
	world.objects.set(objectKey(kind, id), next);
	return json(answer(next));
}

/** Serve a label door for `bearer`, or null when `url` is none of them. */
export async function labelDoor(req: Request, url: URL, bearer: string): Promise<Response | null> {
	const path = url.pathname;
	if (path === '/__mock/labels' && req.method === 'POST') {
		const b = (await body(req)) as {
			bearer?: string;
			definitions?: Partial<Definition>[];
			objects?: Partial<Labelled>[];
			warehouses?: Record<string, string>;
		};
		const world = worldOf(b.bearer ?? bearer);
		for (const [warehouse, project] of Object.entries(b.warehouses ?? {}))
			world.warehouses.set(warehouse, project);
		for (const d of b.definitions ?? []) {
			const res = define(world, d.project ?? '', d as Record<string, unknown>);
			if (!res.ok) return res;
		}
		for (const o of b.objects ?? []) {
			const kind = o.kind ?? 'table';
			const id = o.id ?? '';
			world.objects.set(objectKey(kind, id), {
				kind,
				id,
				uuid: o.uuid ?? `uuid-${kind}-${id}`,
				project: o.project ?? '',
				labels: o.labels ?? {},
			});
		}
		return json({ ok: true });
	}

	const world = worldOf(bearer);
	let m = /^\/management\/v1\/projects\/([^/]+)\/labels$/.exec(path);
	if (m && req.method === 'GET') {
		const project = decodeURIComponent(m[1] ?? '');
		return json(world.definitions.filter((d) => d.project === project));
	}
	m = /^\/management\/v1\/projects\/([^/]+)\/labels\/define$/.exec(path);
	if (m && req.method === 'POST')
		return define(world, decodeURIComponent(m[1] ?? ''), await body(req));
	m = /^\/management\/v1\/projects\/([^/]+)\/labels\/objects$/.exec(path);
	if (m && req.method === 'GET') {
		const project = decodeURIComponent(m[1] ?? '');
		const label = url.searchParams.get('label');
		const objects = [...world.objects.values()]
			.filter((o) => o.project === project && Object.keys(o.labels).length > 0)
			.filter((o) => label === null || label in o.labels)
			.map(answer);
		return json({ project, objects });
	}
	m = /^\/management\/v1\/projects\/([^/]+)\/labels\/([^/]+)\/delete$/.exec(path);
	if (m && req.method === 'POST') {
		const project = decodeURIComponent(m[1] ?? '');
		const name = decodeURIComponent(m[2] ?? '');
		const held = world.definitions.find((d) => d.project === project && d.name === name);
		if (!held) return problem(404, 1, `no label '${name}' in project '${project}'`);
		const carriers = [...world.objects.values()].filter(
			(o) => o.project === project && name in o.labels,
		);
		if (carriers.length > 0)
			return problem(
				409,
				3,
				`label '${name}' is still attached to ${carriers.length} object${carriers.length === 1 ? '' : 's'}`,
			);
		world.definitions = world.definitions.filter((d) => d !== held);
		return json(held);
	}
	m = /^\/management\/v1\/(table|namespace)\/([^/]+)\/labels\/(describe|set|remove)$/.exec(path);
	if (m && req.method === 'POST') {
		const kind = m[1] as Scope;
		const id = decodeURIComponent(m[2] ?? '');
		if (id.startsWith('lance_catalog$'))
			return problem(400, 13, `${kind} '${id}' belongs to no project`);
		if (m[3] === 'describe') return json(described(world, objectOf(world, kind, id)));
		if (m[3] === 'set') return setLabel(world, kind, id, await body(req));
		const name = (await body(req)).name;
		const held = objectOf(world, kind, id);
		const labels = Object.fromEntries(Object.entries(held.labels).filter(([n]) => n !== name));
		const next = { ...held, labels };
		world.objects.set(objectKey(kind, id), next);
		return json(answer(next));
	}
	return null;
}
