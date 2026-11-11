import { Collapsible as CollapsiblePrimitive } from 'bits-ui';

// Annotated with `typeof`: a bare re-export of a bits primitive infers a type TypeScript cannot
// name in a declaration.
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
