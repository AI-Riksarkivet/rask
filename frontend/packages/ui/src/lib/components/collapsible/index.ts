import { Collapsible as CollapsiblePrimitive } from 'bits-ui';

// `typeof` annotations so svelte-package emits index.d.ts (bare re-exports of bits
// primitives infer a non-portable type and silently skip the declaration).
const Root: typeof CollapsiblePrimitive.Root = CollapsiblePrimitive.Root;
const Trigger: typeof CollapsiblePrimitive.Trigger = CollapsiblePrimitive.Trigger;
const Content: typeof CollapsiblePrimitive.Content = CollapsiblePrimitive.Content;

export {
	Root,
	Trigger,
	Content,
	//
	Root as Collapsible,
	Trigger as CollapsibleTrigger,
	Content as CollapsibleContent,
};
