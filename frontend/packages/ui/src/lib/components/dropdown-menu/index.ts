import { DropdownMenu as BitsDropdownMenu } from 'bits-ui';
import Content from './dropdown-menu-content.svelte';
import Item from './dropdown-menu-item.svelte';
import Label from './dropdown-menu-label.svelte';
import Separator from './dropdown-menu-separator.svelte';

// Annotated with `typeof`: a bare re-export of a bits primitive infers a type TypeScript cannot
// name in a declaration.
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
