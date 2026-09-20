/**
 * A secret reaches a zone as a MOUNTED FILE, re-read per call — never through the environment.
 *
 * [[XC-001]], [[LH-160]]. The estate's rule is the owner's, verbatim: "Never secret through envs.
 * Either from ESO, secret store dapr and STS for zero trust." A web zone has no Dapr sidecar, so its
 * sanctioned path is an ESO-managed Secret taken as a file. Measured 2026-09-20, the render carries 30
 * `secretKeyRef` env entries and 21 of them are these seven zones — 70% of the whole violation, all
 * flowing through one shared module.
 *
 * RE-READING PER CALL IS THE OTHER HALF, and it is what XC-001 actually asks for. A process's
 * environment is fixed at exec, so a rotated Secret can never reach a running pod delivered that way —
 * which is why that row's only other option was a watcher that restarts consumers. A file can simply
 * be read again, so the rotation lands with no restart and no reloader in the chart.
 *
 * NO FALLBACK TO AN ENV VALUE, deliberately and per the rule's own words ("never a fallback chain").
 * An unset path is the dev case and yields `undefined`, which is the documented auth-OFF behaviour the
 * zones already have; it is not an invitation to look in `env` for the same secret.
 */

import { mkdtempSync, writeFileSync, rmSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import { readSecretFile } from './secret-file';

const dirs: string[] = [];

function tempSecret(contents: string): string {
	const dir = mkdtempSync(join(tmpdir(), 'rask-secret-'));
	dirs.push(dir);
	const path = join(dir, 'token');
	writeFileSync(path, contents);
	return path;
}

afterEach(() => {
	for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe('readSecretFile', () => {
	it('reads the secret from the mounted file', () => {
		expect(readSecretFile(tempSecret('s3cr3t'))).toBe('s3cr3t');
	});

	it('strips the trailing newline a written file carries', () => {
		// THE DEFECT THIS LEG EXISTS FOR: a token with a trailing \n is a malformed header value, and
		// the failure is a 401 from the upstream rather than anything naming the newline.
		expect(readSecretFile(tempSecret('s3cr3t\n'))).toBe('s3cr3t');
		expect(readSecretFile(tempSecret('s3cr3t\r\n'))).toBe('s3cr3t');
	});

	it('keeps interior whitespace, trimming only the end', () => {
		// A secret is opaque; trimming more than the line ending would silently corrupt a valid value.
		expect(readSecretFile(tempSecret('a b\tc\n'))).toBe('a b\tc');
	});

	it('is undefined when no path is configured', () => {
		expect(readSecretFile(undefined)).toBeUndefined();
		expect(readSecretFile('')).toBeUndefined();
	});

	it('is undefined when the path is configured but absent', () => {
		// A pod that starts before its ESO Secret syncs must come up auth-OFF rather than crash — the
		// zone already treats an unset secret that way, and a missing mount is the same condition.
		expect(readSecretFile(join(tmpdir(), 'rask-secret-does-not-exist', 'token'))).toBeUndefined();
	});

	it('is undefined when the file cannot be read', () => {
		const path = tempSecret('s3cr3t');
		chmodSync(path, 0o000);
		// Unreadable is the same answer as absent: the zone must not crash on a permission change.
		expect(readSecretFile(path)).toBeUndefined();
		chmodSync(path, 0o600);
	});

	it('RE-READS on every call, so a rotation reaches a running pod', () => {
		// The whole point of XC-001's mechanism (2). Delivered through the environment this is
		// impossible — the value is fixed at exec — which is why the alternative was a reloader.
		const path = tempSecret('old');
		expect(readSecretFile(path)).toBe('old');

		writeFileSync(path, 'rotated');

		expect(readSecretFile(path)).toBe('rotated');
	});
});
