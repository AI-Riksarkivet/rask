/**
 * The index a catalog list tells a row's project with: the warehouses (each names its project) and
 * the estate bindings (each namespace's warehouse), read through the shared read instances the lists
 * already use, and the projects the caller can see, which say whether a filter names one of them. No
 * door of its own: these are the reads the warehouses and namespaces pages and the filter make.
 */
import { fetchVisibleProjects } from '#lib/remote/warehouses.api.ts';
import { fetchEstateBindings, fetchWarehouses } from './remote/warehouses.api.ts';
import {
	projectIndex,
	readFailure,
	type ProjectIndexState,
	type VisibleProjectsState,
} from './project-filter.ts';

/** The projects the caller can see (`GET /management/v1/projects`), one input of a filter's
 *  verdict (`filterVerdict`): it lists only the projects the caller is a member of, or every project
 *  to an estate observer, so it is never the only word on whether a filter's project exists. */
export class VisibleProjects {
	state = $state.raw<VisibleProjectsState>({ kind: 'loading' });
	/** The ids once read; none while the read is out or after it failed. */
	ids = $derived(this.state.kind === 'ready' ? [...this.state.ids] : []);

	async load(): Promise<void> {
		const res = await fetchVisibleProjects();
		this.state = res.ok
			? { kind: 'ready', ids: new Set(res.data.map((p) => p.project)) }
			: { kind: 'failed', reason: readFailure('projects', res) };
	}
}

export class ProjectIndexRead {
	state = $state.raw<ProjectIndexState>({ kind: 'loading' });
	readonly visible = new VisibleProjects();

	/** The index once both reads answered; null while loading and after either failed, when a row
	 *  shows no project rather than a guessed one. */
	index = $derived(this.state.kind === 'ready' ? this.state.index : null);

	async load(): Promise<void> {
		const [warehouses, bindings] = await Promise.all([
			fetchWarehouses(),
			fetchEstateBindings(),
			this.visible.load(),
		]);
		if (!warehouses.ok)
			this.state = { kind: 'failed', reason: readFailure('warehouses', warehouses) };
		else if (!bindings.ok)
			this.state = { kind: 'failed', reason: readFailure('namespace bindings', bindings) };
		else
			this.state = { kind: 'ready', index: projectIndex(warehouses.data, bindings.data.bindings) };
	}
}
