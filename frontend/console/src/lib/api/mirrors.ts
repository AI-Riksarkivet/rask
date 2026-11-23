// #lib/api/mirrors — a runtime schema held in step with the generated model it mirrors.
//
// A door's answer is parsed at the boundary with a valibot schema, and the door's model is generated
// from its OpenAPI (`generated/*.ts`). `mirrors<Model>()(schema)` ties the two together at type-check
// time, so a model `make openapi` changes fails the check at the schema's own line:
//
//   - every field the schema outputs, nested ones too, is a field of the model, with a type the model
//     declares for it: the console reads nothing the door does not declare;
//   - every value the model allows a field the schema names, absence included, the schema accepts: the
//     console refuses nothing the door may send.
//
// A mirror may leave out fields it does not read. A field whose type the console decides on purpose (a
// picklist narrowed, an open string kept, a defaulted field the door always sends) is named in the
// second type argument, `mirrors<Model, 'field'>()`, beside a comment saying why; a named field that
// matches the model anyway is reported too, so no mark outlives its reason. Zero runtime: the schema is
// returned as it came.
import type * as v from 'valibot';

/** `T` without `null` and `undefined`. */
type Present<T> = Exclude<T, null | undefined>;

/** The keys `T` names, its index signature left out. */
type Named<T> = keyof {
	[K in keyof T as string extends K ? never : number extends K ? never : K]: T[K];
};

/** Whether `K` may be absent from `T`. */
type IsOptional<T, K extends keyof T> = object extends Pick<T, K> ? true : false;

/** The depth the nested-field walk stops at: a recursive model (the access expansion tree) is
 *  mirrored again at each level, so its own schema covers what lies below. */
type Depth = [never, 0, 1, 2, 3, 4];

/** The dotted path of `key` under `path`. */
type Join<P extends string, K extends string> = P extends '' ? K : `${P}.${K}`;

type Unmodelled<P extends string> = `${P} is not a field of the model`;
type Wide<P extends string> = `${P} parses to a type the model does not declare`;

/** Where an output value at `P` leaves its model's: a field the model does not name, or a value it does
 *  not allow, at any depth through objects and arrays. A field the output leaves out is no drift. */
type Outside<O, M, P extends string, D extends number = 5> = [D] extends [never]
	? never
	: unknown extends M
		? never
		: unknown extends O
			? Wide<P>
			:
					| (null extends O ? (null extends M ? never : Wide<P>) : never)
					| (undefined extends O ? (undefined extends M ? never : Wide<P>) : never)
					| PresentOutside<Present<O>, Present<M>, P, D>;

type PresentOutside<O, M, P extends string, D extends number> = [O] extends [never]
	? never
	: [M] extends [readonly (infer MI)[]]
		? [O] extends [readonly (infer OI)[]]
			? Outside<OI, MI, `${P}[]`, Depth[D]>
			: Wide<P>
		: [O] extends [readonly unknown[]]
			? Wide<P>
			: [M] extends [object]
				? [O] extends [object]
					? Fields<O, M, P, D>
					: Wide<P>
				: [O] extends [M]
					? never
					: Wide<P>;

/** {@link Outside} for each field of an object, and for the values of an open record. */
type Fields<O, M, P extends string, D extends number> =
	| {
			[K in Named<O> & string]: K extends keyof M
				? Outside<O[K], M[K], Join<P, K>, Depth[D]>
				: Unmodelled<Join<P, K>>;
	  }[Named<O> & string]
	| (string extends keyof O
			? string extends keyof M
				? Outside<O[string], M[string], Join<P, '*'>, Depth[D]>
				: Wide<P>
			: never);

/** {@link Fields} at the top, where a field marked as the console's own is held only to being modelled. */
type OutsideTop<O, M, Own> = {
	[K in Named<O> & string]: K extends keyof M
		? K extends Own
			? Extract<Outside<O[K], M[K], K>, Unmodelled<string>>
			: Outside<O[K], M[K], K>
		: Unmodelled<K>;
}[Named<O> & string];

/** The schema's fields its model names too. */
type Shared<I, M> = Named<I> & keyof M & string;

/** Fields the schema requires and the model may leave out. */
type Demanded<I, M, Own> = {
	[K in Shared<I, M>]: K extends Own
		? never
		: IsOptional<I, K> extends true
			? never
			: IsOptional<M, K> extends true
				? K
				: never;
}[Shared<I, M>];

/** Fields where the model allows a value the schema refuses, absence aside. */
type Narrower<I, M, Own> = {
	[K in Shared<I, M>]: K extends Own | Demanded<I, M, Own>
		? never
		: [M[K]] extends [I[K]]
			? never
			: K;
}[Shared<I, M>];

/** Fields marked as the console's own that would pass unmarked. */
type Unneeded<I, O, M, Own> = {
	[K in Named<O> & keyof M & string]: K extends Own
		? [Outside<O[K], M[K], K> | Extract<Demanded<I, M, never> | Narrower<I, M, never>, K>] extends [
				never,
			]
			? K
			: never
		: never;
}[Named<O> & keyof M & string];

/** Every drift, as a sentence that names its field. */
type Drift<I, O, M, Own> =
	| OutsideTop<O, M, Own>
	| `${Demanded<I, M, Own>} is required here and optional in the model`
	| `${Narrower<I, M, Own>} refuses a value the model allows`
	| `${Unneeded<I, O, M, Own>} is marked as the console's own but matches the model`;

/** Each drift as a property the schema lacks, which the type error at the call names. Empty when the
 *  schema mirrors its model. */
type Verdict<I, O, M, Own> = { readonly [K in Drift<I, O, M, Own>]: true };

/** Declare `schema` a mirror of the generated model `M`; `Own` names the fields whose type the console
 *  decides on purpose. */
export function mirrors<M extends object, Own extends keyof M = never>() {
	return <S extends v.GenericSchema>(
		schema: S & Verdict<v.InferInput<S>, v.InferOutput<S>, M, Own>,
	): S => schema;
}
