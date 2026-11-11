/**
 * What a drop dialog says about recovery, read when the dialog opens: the warehouse's delete profile
 * for the object about to be dropped (`deleteProfileOf`), as a `DropRecovery`. One read at a time
 * answers: a dialog opened for another object, or opened again, retires the answer still in flight.
 */
import { deleteProfileOf, dropRecovery, type DropRecovery } from './drop-recovery';

export class DropRecoveryRead {
	/** `null` while the profile is being read. */
	current = $state.raw<DropRecovery | null>(null);
	#generation = 0;

	async load(id: string): Promise<void> {
		const generation = ++this.#generation;
		this.current = null;
		const profile = await deleteProfileOf(id);
		if (generation === this.#generation) this.current = dropRecovery(profile, Date.now());
	}
}
