/**
 * The tiers a list's namespaces declare, read once per namespace through the shared `namespaceTier`
 * read (cached per namespace, so a list and a page that both ask send one DescribeNamespace each). A
 * list keeps one of these and loads it with the namespaces its rows name; a namespace it has not read
 * yet shows no tier rather than a guessed one.
 */
import { namespaceTier } from './remote/namespace.api';
import type { StageInfo } from './stage';

export class NamespaceTiers {
	#tiers = $state.raw<ReadonlyMap<string, StageInfo | null>>(new Map());

	/** The tier `namespace` declares, or null: none, or not read yet. */
	of(namespace: string): StageInfo | null {
		return this.#tiers.get(namespace) ?? null;
	}

	async load(namespaces: Iterable<string>): Promise<void> {
		const unique = [...new Set(namespaces)];
		const answers = await Promise.all(
			unique.map(async (namespace) => [namespace, await namespaceTier(namespace)] as const),
		);
		this.#tiers = new Map(answers);
	}
}
