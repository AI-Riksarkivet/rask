import Root from './dialog.svelte';
import Trigger from './dialog-trigger.svelte';
import Content from './dialog-content.svelte';
import Title from './dialog-title.svelte';
import Description from './dialog-description.svelte';

export const Dialog = { Root, Trigger, Content, Title, Description };
export {
	Root as DialogRoot,
	Trigger as DialogTrigger,
	Content as DialogContent,
	Title as DialogTitle,
	Description as DialogDescription,
};
