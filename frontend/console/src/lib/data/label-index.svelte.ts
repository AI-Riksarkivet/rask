/**
 * The labels a catalog list shows and filters by: each project the caller can see is asked for its
 * labelled objects (`GET /management/v1/projects/{p}/labels/objects`), through the shared read the
 * label writes refresh. A project the caller is no member of answers nothing here, so a row in it
 * shows no label: the list says which projects' labels could not be read rather than showing their
 * rows as unlabelled without a word.
 */
import { fetchLabelledObjects } from './remote/labels.api.ts';
import { objectKey, type LabelScope, type Labels } from './labels.ts';
import { readFailure, type VisibleProjectsState } from './project-filter.ts';

/** A project whose labelled objects could not be read, and why; `project` is null when the projects
 *  to ask could not be read themselves. */
export type LabelReadFailure = { project: string | null; reason: string };

export type LabelIndexState =
	| { kind: 'loading' }
	| {
			kind: 'ready';
			byObject: ReadonlyMap<string, Labels>;
			names: readonly string[];
			failures: readonly LabelReadFailure[];
	  };

const NONE: Labels = {};

export class LabelIndexRead {
	state = $state.raw<LabelIndexState>({ kind: 'loading' });

	/** The label names some object the caller can see carries; none until read. */
	names = $derived(this.state.kind === 'ready' ? this.state.names : []);

	/** The labels `id` carries, as far as the index knows; none until read. */
	of(kind: LabelScope, id: string): Labels {
		return this.state.kind === 'ready'
			? (this.state.byObject.get(objectKey(kind, id)) ?? NONE)
			: NONE;
	}

	/** Read the labelled objects of every project the caller can see, once that read has answered. */
	async load(visible: VisibleProjectsState): Promise<void> {
		if (visible.kind === 'loading') return;
		if (visible.kind === 'failed') {
			this.state = {
				kind: 'ready',
				byObject: new Map(),
				names: [],
				failures: [{ project: null, reason: visible.reason }],
			};
			return;
		}
		const projects = [...visible.ids];
		const answers = await Promise.all(
			projects.map(async (project) => ({ project, res: await fetchLabelledObjects(project) })),
		);
		const byObject = new Map<string, Labels>();
		const names = new Set<string>();
		const failures: LabelReadFailure[] = [];
		for (const { project, res } of answers) {
			if (!res.ok) {
				failures.push({ project, reason: readFailure('labels', res) });
				continue;
			}
			for (const o of res.data.objects) {
				byObject.set(objectKey(o.kind, o.id), o.labels);
				for (const name of Object.keys(o.labels)) names.add(name);
			}
		}
		this.state = { kind: 'ready', byObject, names: [...names].sort(), failures };
	}
}
