/** Maps each node `type` to its Svelte component. Passed to <SvelteFlow>.
 *  Module scope on purpose — an inline object would remount every node on
 *  each render (svelte-flow rule 5). */
import type { NodeTypes } from '@xyflow/svelte';
import ImageNode from './nodes/ImageNode.svelte';
import TextNode from './nodes/TextNode.svelte';
import DatasetNode from './nodes/DatasetNode.svelte';
import PromptNode from './nodes/PromptNode.svelte';
import ModelNode from './nodes/ModelNode.svelte';
import McpNode from './nodes/McpNode.svelte';
import AltoNode from './nodes/AltoNode.svelte';
import ExtractNode from './nodes/ExtractNode.svelte';
import RegexNode from './nodes/RegexNode.svelte';
import CompareNode from './nodes/CompareNode.svelte';
import InspectNode from './nodes/InspectNode.svelte';

export const nodeTypes: NodeTypes = {
	image: ImageNode,
	text: TextNode,
	dataset: DatasetNode,
	prompt: PromptNode,
	model: ModelNode,
	mcp: McpNode,
	alto: AltoNode,
	extract: ExtractNode,
	regex: RegexNode,
	compare: CompareNode,
	inspect: InspectNode,
};
