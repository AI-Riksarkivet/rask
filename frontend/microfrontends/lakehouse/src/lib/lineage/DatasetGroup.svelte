<script lang="ts" module>
	import type { Node, NodeProps } from '@xyflow/svelte';

	export interface DatasetGroupData extends Record<string, unknown> {
		/** The table this container holds the columns of. */
		dataset: string;
		/** True for the dataset the column view is rooted on. */
		isRoot: boolean;
		/** How many of its columns are drawn — not how many it has, which this view never knows. */
		count: number;
	}

	export type DatasetGroupType = Node<DatasetGroupData, 'dsgroup'>;
</script>

<script lang="ts">
	/**
	 * The table boundary a column-level graph needs and did not have.
	 *
	 * Columns used to be flat siblings carrying their table name as a subtitle, so nothing drew the
	 * table itself. Measured on `acme-silver$features`: 23 column nodes, 3 distinct datasets, and ONE
	 * stack at x=20 holding columns of `acme-silver$features` and `acme-bronze$events` interleaved —
	 * two tables' fields in one column, distinguishable only by reading every subtitle. A field-level
	 * graph whose whole subject is "which table's field feeds which" cannot leave the tables implicit.
	 *
	 * Marquez nests columns inside a compound dataset node and lays the pair out in one ELK pass
	 * (`column-level/layout.ts` + `hierarchyHandling: INCLUDE_CHILDREN`); this is the same shape via
	 * Svelte Flow sub-flows — `parentId` + `extent: 'parent'` on the children, the box here.
	 *
	 * It renders BEHIND its children (Svelte Flow draws a parent before the nodes it contains) and
	 * must not swallow their clicks, hence `pointer-events` only on the header strip.
	 */
	let { data }: NodeProps<DatasetGroupType> = $props();

	/** The table's own name, without the namespace prefix the estate qualifies it with. */
	const short = $derived(data.dataset.includes('$') ? data.dataset.split('$').pop() : data.dataset);
</script>

<div class="group" class:root={data.isRoot}>
	<header class="ghead" title={data.dataset}>
		<span class="gname">{short}</span>
		<span class="gcount">{data.count}</span>
	</header>
</div>

<style>
	.group {
		width: 100%;
		height: 100%;
		border: 1px solid var(--line);
		border-radius: 10px;
		/* Barely tinted: this is a boundary, not a surface. A filled container competes with the
		   cards inside it, which are the thing being read. */
		background: color-mix(in srgb, var(--panel-2) 45%, transparent);
		pointer-events: none;
	}
	.group.root {
		border-color: color-mix(in srgb, var(--primary) 55%, var(--line));
		background: color-mix(in srgb, var(--primary) 7%, transparent);
	}
	.ghead {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 5px 10px 0;
		pointer-events: auto;
	}
	.gname {
		flex: 1;
		min-width: 0;
		font-size: 11px;
		font-weight: 600;
		color: var(--ink);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.group.root .gname {
		color: var(--primary);
	}
	.gcount {
		flex: none;
		font-size: 9px;
		padding: 0 4px;
		border-radius: 4px;
		color: var(--muted);
		background: color-mix(in srgb, var(--ink) 8%, transparent);
	}
</style>
