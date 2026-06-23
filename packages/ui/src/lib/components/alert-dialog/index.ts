import { AlertDialog as BitsAlertDialog } from 'bits-ui';
import Overlay from './alert-dialog-overlay.svelte';
import Content from './alert-dialog-content.svelte';
import Title from './alert-dialog-title.svelte';
import Description from './alert-dialog-description.svelte';
import Action from './alert-dialog-action.svelte';
import Cancel from './alert-dialog-cancel.svelte';

// Annotate with `typeof` so the emitted .d.ts can name these types (otherwise
// svelte-package's declaration emit silently skips index.d.ts and publint fails).
const Root: typeof BitsAlertDialog.Root = BitsAlertDialog.Root;
const Trigger: typeof BitsAlertDialog.Trigger = BitsAlertDialog.Trigger;
const Portal: typeof BitsAlertDialog.Portal = BitsAlertDialog.Portal;

export const AlertDialog = {
	Root,
	Trigger,
	Portal,
	Overlay,
	Content,
	Title,
	Description,
	Action,
	Cancel,
};

export {
	Root,
	Trigger,
	Portal,
	Overlay,
	Content,
	Title,
	Description,
	Action,
	Cancel,
	//
	Root as AlertDialogRoot,
	Trigger as AlertDialogTrigger,
	Portal as AlertDialogPortal,
	Overlay as AlertDialogOverlay,
	Content as AlertDialogContent,
	Title as AlertDialogTitle,
	Description as AlertDialogDescription,
	Action as AlertDialogAction,
	Cancel as AlertDialogCancel,
};
