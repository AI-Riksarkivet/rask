/**
 * The console's data layer: three browser-side builders for the console's data functions —
 * `query` (a cached, refreshable read), `query.live` (a cursor that advances) and `command` (a
 * write). Each `*.api.ts` module declares its reads and writes with them, and every one reaches its
 * service's door same-origin with the signed-in person's bearer. A component holds a read as
 * `await read()`, `read().current`, `read().refresh()`.
 *
 * Semantics a caller relies on:
 *  - Calling a read with the same arguments returns the SAME instance, so a write's
 *    `void read().refresh()` updates every holder of that read.
 *  - A call deduplicates a burst (a read settled within {@link FRESH_MS} is reused) and otherwise
 *    re-reads, so opening a page shows the estate as it is now rather than a cached answer.
 *  - A failing read rejects (`error(403, …)` included) and leaves `.current` as it was.
 *  - Calling a read never makes the CALLER depend on the read's own bookkeeping: an effect that calls
 *    `read()` must not re-run because that read started or settled (its `loading`), or a refresh from
 *    anywhere would re-run every effect that once asked for it.
 */
import { error } from '@sveltejs/kit';
import { untrack } from 'svelte';
import { auth, type Session } from './auth.svelte';

/** A read settled this recently is reused by the next call instead of read again. */
const FRESH_MS = 1000;

type Validated<Out> =
	| { value: Out; issues?: undefined }
	| { issues: readonly { message: string }[] };

/** The Standard Schema surface (valibot implements it): what a caller passes is the schema's INPUT,
 *  what the function receives is its OUTPUT, defaults applied. */
type StandardSchema<In, Out> = {
	readonly '~standard': {
		validate: (value: unknown) => Validated<Out> | Promise<Validated<Out>>;
		readonly types?: { readonly input: In; readonly output: Out } | undefined;
	};
};

async function validated<Out>(
	schema: StandardSchema<unknown, Out> | 'unchecked',
	input: unknown,
): Promise<Out> {
	if (schema === 'unchecked') return input as Out;
	const result = await schema['~standard'].validate(input);
	if (result.issues) error(400, result.issues.map((issue) => issue.message).join('; '));
	return result.value;
}

/** What a data function reaches for: the browser's fetch and the signed-in person. */
export interface RequestEvent {
	fetch: typeof globalThis.fetch;
	locals: { session: Session | null; authEnabled: boolean };
}

export function getRequestEvent(): RequestEvent {
	return {
		fetch: globalThis.fetch.bind(globalThis),
		locals: { session: auth.session, authEnabled: auth.enabled },
	};
}

/** One read, held. It is a Promise of its latest read; what is reactive is `.current`, `.error` and
 *  `.loading`. The promise itself is a plain field, so an effect that awaits a read does not come to
 *  depend on it and re-run when the read is refreshed elsewhere. */
class Query<T> implements Promise<T> {
	current = $state.raw<T | undefined>(undefined);
	error = $state.raw<unknown>(undefined);
	loading = $state(false);
	#run: () => Promise<T>;
	#promise: Promise<T>;
	#settledAt = 0;
	/** Which start is the latest. Only the latest writes `current`, `error` and `loading`: a refresh
	 *  started while an older read is in flight supersedes it, and the older answer, however late it
	 *  lands, must not overwrite the newer one or clear `loading` while the newer is still out. */
	#generation = 0;

	constructor(run: () => Promise<T>) {
		this.#run = run;
		this.#promise = this.#start();
	}

	#start(): Promise<T> {
		const generation = ++this.#generation;
		const latest = (): boolean => generation === this.#generation;
		this.loading = true;
		const promise = this.#run().then(
			(value) => {
				if (latest()) {
					this.current = value;
					this.error = undefined;
					this.loading = false;
					this.#settledAt = Date.now();
				}
				return value;
			},
			(err: unknown) => {
				if (latest()) {
					this.error = err;
					this.loading = false;
					this.#settledAt = Date.now();
				}
				throw err;
			},
		);
		// A read nobody awaits must not surface as an unhandled rejection; holders see `.error`.
		promise.catch(() => {});
		return promise;
	}

	/** Read again; holders of this instance see the new value. */
	refresh(): Promise<void> {
		this.#promise = this.#start();
		return this.#promise.then(() => undefined);
	}

	/** Re-read unless a read is in flight or settled within {@link FRESH_MS}. */
	touch(): void {
		if (this.loading || Date.now() - this.#settledAt < FRESH_MS) return;
		this.#promise = this.#start();
	}

	// A read IS a promise of its latest value, as a SvelteKit remote query is: `await read()` is how a
	// page holds it, and that is the point rather than an accident.
	// oxlint-disable-next-line unicorn/no-thenable
	get then(): Promise<T>['then'] {
		const promise = this.#promise;
		return promise.then.bind(promise);
	}

	get catch(): Promise<T>['catch'] {
		const promise = this.#promise;
		return promise.catch.bind(promise);
	}

	get finally(): Promise<T>['finally'] {
		const promise = this.#promise;
		return promise.finally.bind(promise);
	}

	readonly [Symbol.toStringTag] = 'Query';
}

const keyOf = (args: unknown[]): string => JSON.stringify(args);

type Read<A extends unknown[], T> = (...args: A) => Query<T>;

/** Declare a read. With a schema first, the argument is validated before the read runs. */
export function query<T>(run: () => Promise<T>): Read<[], T>;
export function query<In, Out, T>(
	schema: StandardSchema<In, Out>,
	run: (input: Out) => Promise<T>,
): Read<[In], T>;
export function query<I, T>(schema: 'unchecked', run: (input: I) => Promise<T>): Read<[I], T>;
export function query(
	...declared:
		| [() => Promise<unknown>]
		| [StandardSchema<unknown, unknown> | 'unchecked', (input: unknown) => Promise<unknown>]
): Read<unknown[], unknown> {
	// UNBOUNDED, on purpose. Same arguments, same instance is what lets a write's `read().refresh()`
	// reach every holder; evicting an instance a component still holds would leave that holder on a
	// copy no refresh reaches. The map lives one page life (tokens are in memory, so a reload starts
	// a new one) and grows by the distinct argument sets a person opens in it.
	const instances = new Map<string, Query<unknown>>();
	const [schema, run] = declared.length === 1 ? (['unchecked', declared[0]] as const) : declared;
	return (...args: unknown[]) =>
		untrack(() => {
			const key = keyOf(args);
			const held = instances.get(key);
			if (held) {
				held.touch();
				return held;
			}
			const instance = new Query(async () => run(await validated(schema, args[0])));
			instances.set(key, instance);
			return instance;
		});
}

/** One live cursor: the latest value a generator yielded. Awaiting it gives that value (the first,
 *  once it arrives), and `then` is read reactively, so a template's `await live` re-renders on each
 *  new value. */
class LiveQuery<T> implements PromiseLike<T> {
	current = $state.raw<T | undefined>(undefined);
	connected = $state(false);
	error = $state.raw<unknown>(undefined);
	#promise = $state.raw<Promise<T>>(new Promise<T>(() => {}));

	constructor(open: () => AsyncGenerator<T>) {
		let first: { resolve: (value: T) => void; reject: (reason: unknown) => void } | null = null;
		this.#promise = new Promise<T>((resolve, reject) => {
			first = { resolve, reject };
		});
		this.#promise.catch(() => {});
		void this.#pump(open, () => first);
	}

	async #pump(
		open: () => AsyncGenerator<T>,
		pending: () => { resolve: (value: T) => void; reject: (reason: unknown) => void } | null,
	): Promise<void> {
		let started = false;
		try {
			for await (const value of open()) {
				this.current = value;
				this.connected = true;
				if (started) this.#promise = Promise.resolve(value);
				else pending()?.resolve(value);
				started = true;
			}
		} catch (err) {
			this.error = err;
			if (started) {
				const failed = Promise.reject(err);
				failed.catch(() => {});
				this.#promise = failed;
			} else pending()?.reject(err);
		} finally {
			this.connected = false;
		}
	}

	// A read IS a promise of its latest value, as a SvelteKit remote query is: `await read()` is how a
	// page holds it, and that is the point rather than an accident.
	// oxlint-disable-next-line unicorn/no-thenable
	get then(): Promise<T>['then'] {
		const promise = this.#promise;
		return promise.then.bind(promise);
	}
}

/** Declare a live cursor: a generator the browser runs once per argument set and reads from. */
query.live = function live<A extends unknown[], T>(
	open: (...args: A) => AsyncGenerator<T>,
): (...args: A) => LiveQuery<T> {
	// Unbounded for the reason `query`'s map is; every live read declared today takes no argument,
	// so each holds one instance.
	const instances = new Map<string, LiveQuery<T>>();
	return (...args: A) =>
		untrack(() => {
			const key = keyOf(args);
			let instance = instances.get(key);
			if (!instance) {
				instance = new LiveQuery(() => open(...args));
				instances.set(key, instance);
			}
			return instance;
		});
};

/** Declare a write. With a schema first, the argument is validated before the write runs. */
export function command<T>(run: () => Promise<T>): () => Promise<T>;
export function command<In, Out, T>(
	schema: StandardSchema<In, Out>,
	run: (input: Out) => Promise<T>,
): (input: In) => Promise<T>;
export function command<I, T>(
	schema: 'unchecked',
	run: (input: I) => Promise<T>,
): (input: I) => Promise<T>;
export function command(
	...declared:
		| [() => Promise<unknown>]
		| [StandardSchema<unknown, unknown> | 'unchecked', (input: unknown) => Promise<unknown>]
): (input?: unknown) => Promise<unknown> {
	if (declared.length === 1) return declared[0];
	const [schema, run] = declared;
	return async (input?: unknown) => run(await validated(schema, input));
}
