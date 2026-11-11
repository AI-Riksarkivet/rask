import { formatZoned } from '@rask/ui/utils';
import { DELIMITER } from '#lib/api/identifiers.ts';
import type { Warehouse } from './catalog';
import { fetchEstateBindings, fetchWarehouse } from './remote/warehouses.api';

// What a drop under a warehouse does, as the warehouse's record keeps it (`delete_profile`, ADR
// 0146): `soft` keeps the dropped object recoverable for `grace_days`, `hard` destroys it at once,
// and `estate` (or no profile) takes the estate's own grace period, which no catalog door publishes.
// A drop dialog says which, so "permanently" appears only where it is true.

type DeleteProfile = Warehouse['delete_profile'];

export type DropRecovery =
	| { kind: 'recoverable'; until: number }
	| { kind: 'permanent' }
	| { kind: 'estate-default' }
	| { kind: 'unknown' };

const DAY_MS = 86_400_000;

/** The recovery a drop made at `now` gets under `profile`; `undefined` is a profile that could not be read. */
export function dropRecovery(profile: DeleteProfile | undefined, now: number): DropRecovery {
	if (profile === undefined) return { kind: 'unknown' };
	if (profile === null || profile.mode === 'estate') return { kind: 'estate-default' };
	if (profile.mode === 'hard') return { kind: 'permanent' };
	const days = profile.grace_days ?? 0;
	return days > 0
		? { kind: 'recoverable', until: now + days * DAY_MS }
		: { kind: 'estate-default' };
}

/** The clause a drop dialog ends its sentence with. */
export function dropRecoveryText(recovery: DropRecovery): string {
	switch (recovery.kind) {
		case 'recoverable':
			return `recoverable until ${formatZoned(recovery.until)}, then removed for good`;
		case 'permanent':
			return 'permanently: this warehouse keeps no dropped objects';
		case 'estate-default':
			return "recoverable only if the estate keeps drops: this warehouse takes the estate's grace period, which the catalog does not publish";
		case 'unknown':
			return 'whether it stays recoverable could not be read from its warehouse';
	}
}

/** The delete profile of the warehouse a catalog object lives under, found through the binding of its
 *  top-level namespace; `undefined` when the binding or the warehouse could not be read. */
export async function deleteProfileOf(id: string): Promise<DeleteProfile | undefined> {
	const top = id.split(DELIMITER)[0] ?? id;
	const bindings = await fetchEstateBindings();
	const warehouse = bindings.ok ? bindings.data.bindings[top] : undefined;
	if (warehouse === undefined) return undefined;
	const record = await fetchWarehouse(warehouse);
	return record.ok ? (record.data.delete_profile ?? null) : undefined;
}
