/**
 * The index a catalog list tells a row's project with: the warehouses (each names its project) and
 * the estate bindings (each namespace's warehouse), read through the shared read instances the lists
 * already use. No door of its own: these are the reads the warehouses and namespaces pages make.
 */
import { fetchEstateBindings, fetchWarehouses } from './remote/warehouses.api.ts';
import { projectIndex, readFailure, type ProjectIndexState } from './project-filter.ts';

export class ProjectIndexRead {
	state = $state.raw<ProjectIndexState>({ kind: 'loading' });

	/** The index once both reads answered; null while loading and after either failed, when a row
	 *  shows no project rather than a guessed one. */
	index = $derived(this.state.kind === 'ready' ? this.state.index : null);

	async load(): Promise<void> {
		const [warehouses, bindings] = await Promise.all([fetchWarehouses(), fetchEstateBindings()]);
		if (!warehouses.ok)
			this.state = { kind: 'failed', reason: readFailure('warehouses', warehouses) };
		else if (!bindings.ok)
			this.state = { kind: 'failed', reason: readFailure('namespace bindings', bindings) };
		else
			this.state = { kind: 'ready', index: projectIndex(warehouses.data, bindings.data.bindings) };
	}
}
