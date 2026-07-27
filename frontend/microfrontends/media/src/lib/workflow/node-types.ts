/** Maps each node `type` to its Svelte component. Passed to <SvelteFlow>. */
import type { NodeTypes } from '@xyflow/svelte';
import QueryNode from './nodes/QueryNode.svelte';
import ImageNode from './nodes/ImageNode.svelte';
import FilterNode from './nodes/FilterNode.svelte';
import AtlasNode from './nodes/AtlasNode.svelte';
import SearchNode from './nodes/SearchNode.svelte';
import CombineNode from './nodes/CombineNode.svelte';
import TaggerNode from './nodes/TaggerNode.svelte';
import ResultsNode from './nodes/ResultsNode.svelte';
import ExportNode from './nodes/ExportNode.svelte';

export const nodeTypes: NodeTypes = {
	query: QueryNode,
	image: ImageNode,
	filter: FilterNode,
	atlas: AtlasNode,
	search: SearchNode,
	combine: CombineNode,
	tagger: TaggerNode,
	results: ResultsNode,
	export: ExportNode,
};
