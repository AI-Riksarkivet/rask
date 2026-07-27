import { Popover as BitsPopover } from 'bits-ui';
import Content from './popover-content.svelte';

// The bits-ui popover, wrapped exactly like `dropdown-menu`: the primitives pass through, only
// Content is skinned (portalled, shadcn surface classes). A popover — not a menu — is what a panel
// of ROWS wants: a menu makes every child a `menuitem` with typeahead and arrow navigation, and
// closes on any click inside, which is wrong for a list whose rows carry their own dismiss button.
//
// Annotate with `typeof` so the emitted .d.ts can name these types (otherwise svelte-package's
// declaration emit silently skips index.d.ts and publint fails).
const Root: typeof BitsPopover.Root = BitsPopover.Root;
const Trigger: typeof BitsPopover.Trigger = BitsPopover.Trigger;
const Close: typeof BitsPopover.Close = BitsPopover.Close;

export {
	Root,
	Trigger,
	Close,
	Content,
	//
	Root as Popover,
	Trigger as PopoverTrigger,
	Close as PopoverClose,
	Content as PopoverContent,
};
