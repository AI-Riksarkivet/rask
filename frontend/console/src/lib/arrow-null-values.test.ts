/**
 * `patches/apache-arrow@21.2.0.patch` replaces apache-arrow's compiled null check (`new Function`, which
 * the console's CSP refuses) with a closure. This holds the closure to the compiled function's answers.
 *
 * `compiled` is upstream's `createIsValidFunction` body from apache-arrow 21.2.0 `builder/valid.mjs`,
 * verbatim but for its name, over the package's own `valueToString`: the reference the patch must
 * agree with on every null value and every input below.
 */
import { describe, expect, it } from 'vitest';
import { createIsValidFunction } from 'apache-arrow/builder/valid';
import { valueToString } from 'apache-arrow/util/pretty';

function valueToCase(x: unknown): string {
	if (typeof x !== 'bigint') return valueToString(x);
	return `${valueToString(x)}n`;
}

function compiled(nullValues: unknown[]): (x: unknown) => boolean {
	let fnBody = '';
	const noNaNs = nullValues.filter((x) => x === x);
	if (noNaNs.length > 0) {
		fnBody = `
    switch (x) {${noNaNs
			.map(
				(x) => `
        case ${valueToCase(x)}:`,
			)
			.join('')}
            return false;
    }`;
	}
	if (nullValues.length !== noNaNs.length) fnBody = `if (x !== x) return false;\n${fnBody}`;
	return new Function(`x`, `${fnBody}\nreturn true;`) as (x: unknown) => boolean;
}

const shared = { k: 1 };
const date = new Date(0);
const primitive = { [Symbol.toPrimitive]: () => '7' };

/** The null values a builder may be given, one kind each. */
const NULL_VALUES: [string, unknown][] = [
	['null', null],
	['undefined', undefined],
	['NaN', Number.NaN],
	['zero', 0],
	['negative zero', -0],
	['a fraction', 1.5],
	['Infinity', Number.POSITIVE_INFINITY],
	['a bigint', 10n],
	['true', true],
	['a string', 'n/a'],
	['the empty string', ''],
	['a string with a quote escape', 'a\\"b'],
	['a string with a tab', 'a\tb'],
	['a string with a backslash before a tab', 'a\\\tb'],
	['a string with a line continuation', 'a\\\nb'],
	['a string with a hex escape', 'a\\x41'],
	['a string with a code point escape', 'a\\u{1F600}'],
	['a string with a legacy octal escape', 'a\\101'],
	['an object', shared],
	['an array', [1]],
	['a typed array', new Uint8Array([1])],
	['a date', date],
	['an object with toPrimitive', primitive],
];

/** Every input, null values included, so each is checked against itself and against the others. */
const INPUTS: unknown[] = [
	...NULL_VALUES.map(([, value]) => value),
	'1',
	1,
	7,
	'7',
	1n,
	false,
	'a"b',
	'ab',
	'aA',
	'a\u{1F600}',
	'1970-01-01T00:00:00.000Z',
	{ k: 1 },
];

/** Each input's answer, or the error the check could not be built past (a date's case is its
 *  `toString()` text, which does not compile). */
function answers(build: () => (x: unknown) => boolean): boolean[] | string {
	let check: (x: unknown) => boolean;
	try {
		check = build();
	} catch (err) {
		return err instanceof Error ? err.name : String(err);
	}
	return INPUTS.map((x) => check(x));
}

describe('the patched createIsValidFunction', () => {
	it.each(NULL_VALUES)('answers as the compiled check for %s', (_, nullValue) => {
		expect(answers(() => createIsValidFunction([nullValue]))).toEqual(
			answers(() => compiled([nullValue])),
		);
	});

	it('refuses at creation a case that is no literal, which the compiled check evaluates when called', () => {
		const expression = { [Symbol.toPrimitive]: () => 'missing' };
		expect(() => compiled([expression])).not.toThrow();
		expect(() => createIsValidFunction([expression])).toThrow(SyntaxError);
	});

	it('answers as the compiled check for several null values at once', () => {
		const nullValues = [null, 'n/a', Number.NaN, shared];
		expect(answers(() => createIsValidFunction(nullValues))).toEqual(
			answers(() => compiled(nullValues)),
		);
	});
});
