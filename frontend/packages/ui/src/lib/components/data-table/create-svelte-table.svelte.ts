import {
	createTable,
	type RowData,
	type TableOptions,
	type TableOptionsResolved,
	type TableState,
	type Updater,
} from '@tanstack/table-core';

/**
 * A reactive TanStack table for Svelte 5 — the shadcn-svelte adapter pattern over
 * `@tanstack/table-core`: the caller passes getter-based `data`/`columns`/`state` (so runes
 * reactivity flows through), internal state lives in `$state`, and `$effect.pre` re-syncs the
 * options before paint.
 *
 * @example
 * ```svelte
 * <script lang="ts">
 *   const table = createSvelteTable({
 *     get data() { return rows; },
 *     get columns() { return columns; },
 *     getCoreRowModel: getCoreRowModel(),
 *   });
 * </script>
 * ```
 */
export function createSvelteTable<TData extends RowData>(options: TableOptions<TData>) {
	const resolvedOptions: TableOptionsResolved<TData> = mergeObjects(
		{
			state: {},
			onStateChange() {},
			renderFallbackValue: null,
			mergeOptions: (
				defaultOptions: TableOptions<TData>,
				newOptions: Partial<TableOptions<TData>>,
			) => {
				return mergeObjects(defaultOptions, newOptions);
			},
		},
		options,
	);

	const table = createTable(resolvedOptions);
	let state = $state<TableState>(table.initialState);

	function updateOptions() {
		table.setOptions(() => {
			return mergeObjects(resolvedOptions, options, {
				state: mergeObjects(state, options.state || {}),

				onStateChange: (updater: Updater<TableState>) => {
					if (updater instanceof Function) state = updater(state);
					else state = mergeObjects(state, updater);

					options.onStateChange?.(updater);
				},
			});
		});
	}

	updateOptions();

	$effect.pre(() => {
		updateOptions();
	});

	return table;
}

type MaybeThunk<T extends object> = T | (() => T | null | undefined);
type Intersection<T extends readonly unknown[]> = (T extends [infer H, ...infer R]
	? H & Intersection<R>
	: unknown) &
	Record<string, never>;

/**
 * Lazily merge several objects (or thunks) while preserving getter semantics from every source —
 * later sources win. Proxy-based so a caller's `get data()` getters are read on ACCESS (keeping
 * them reactive), never eagerly copied.
 */
export function mergeObjects<Sources extends readonly MaybeThunk<object>[]>(
	...sources: Sources
): Intersection<{ [K in keyof Sources]: Sources[K] }> {
	const resolve = <T extends object>(src: MaybeThunk<T>): T | undefined =>
		typeof src === 'function' ? (src() ?? undefined) : src;

	const findSourceWithKey = (key: PropertyKey) => {
		for (let i = sources.length - 1; i >= 0; i--) {
			const obj = resolve(sources[i]!);
			if (obj && key in obj) return obj;
		}
		return undefined;
	};

	return new Proxy(Object.create(null), {
		get(_, key) {
			const src = findSourceWithKey(key);
			return src?.[key as never];
		},

		has(_, key) {
			return !!findSourceWithKey(key);
		},

		ownKeys(): (string | symbol)[] {
			const all: (string | symbol)[] = [];
			for (const s of sources) {
				const obj = resolve(s);
				if (obj) {
					for (const k of Reflect.ownKeys(obj)) {
						if (!all.includes(k)) all.push(k);
					}
				}
			}
			return all;
		},

		getOwnPropertyDescriptor(_, key) {
			const src = findSourceWithKey(key);
			if (!src) return undefined;
			return {
				configurable: true,
				enumerable: true,
				value: (src as Record<PropertyKey, unknown>)[key],
				writable: true,
			};
		},
	}) as Intersection<{ [K in keyof Sources]: Sources[K] }>;
}
