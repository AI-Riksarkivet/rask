/**
 * Reading a secret from a MOUNTED FILE — the sanctioned delivery for a pod with no Dapr sidecar.
 *
 * The estate's rule is the owner's, verbatim: "Never secret through envs. Either from ESO, secret
 * store dapr and STS for zero trust." A web zone carries no sidecar, so its path is an ESO-managed
 * Secret taken as a file. What travels in the environment is the PATH, which is not a secret.
 *
 * RE-READ PER CALL, and that is the reason this exists rather than a module-level constant. A
 * process's environment is fixed at exec, so a rotated Secret can never reach a pod that took it that
 * way — [[XC-001]]'s only other option was installing a watcher to restart consumers. kubelet updates
 * a projected Secret volume in place, so reading the file again is the whole mechanism: the rotation
 * lands with no restart and no reloader in the chart. The cost is one small synchronous read on a
 * tmpfs mount per call, which is why this is not cached.
 *
 * NO FALLBACK TO AN ENV VALUE. The rule names fallback chains explicitly, and a chain would make the
 * banned path reachable again by unsetting one variable — the failure mode being that everything keeps
 * working, so nobody notices the secret went back through the environment.
 */

import { readFileSync } from 'node:fs';

/**
 * The secret at `path`, or `undefined` when there is nothing readable there.
 *
 * ABSENT AND UNREADABLE ANSWER THE SAME WAY, on purpose: a zone whose Secret has not synced yet must
 * come up auth-OFF rather than crash-loop, which is exactly how it already treats an unset secret. A
 * pod that dies because ESO is a few seconds behind turns a transient into an outage.
 *
 * The trailing line ending is stripped because a written file carries one and a token with `\n` in it
 * is a malformed header value — the resulting 401 names nothing about the newline. Interior whitespace
 * is kept: a secret is opaque, and trimming more than the line ending would corrupt a valid value.
 */
export function readSecretFile(path: string | undefined): string | undefined {
	if (!path) return undefined;
	try {
		return readFileSync(path, 'utf8').replace(/\r?\n$/, '');
	} catch {
		return undefined;
	}
}
