import { DropdownMenu as BitsDropdownMenu } from 'bits-ui';
import Content from './dropdown-menu-content.svelte';
import Item from './dropdown-menu-item.svelte';
import Label from './dropdown-menu-label.svelte';
import Separator from './dropdown-menu-separator.svelte';

// Annotate with `typeof` so the emitted .d.ts can name these types (otherwise
// svelte-package's declaration emit silently skips index.d.ts and publint fails).
const Root: typeof BitsDropdownMenu.Root = BitsDropdownMenu.Root;
const Trigger: typeof BitsDropdownMenu.Trigger = BitsDropdownMenu.Trigger;
const Group: typeof BitsDropdownMenu.Group = BitsDropdownMenu.Group;

export {
	Root,
	Trigger,
	Group,
	Content,
	Item,
	Label,
	Separator,
	//
	Root as DropdownMenu,
	Trigger as DropdownMenuTrigger,
	Group as DropdownMenuGroup,
	Content as DropdownMenuContent,
	Item as DropdownMenuItem,
	Label as DropdownMenuLabel,
	Separator as DropdownMenuSeparator,
};
