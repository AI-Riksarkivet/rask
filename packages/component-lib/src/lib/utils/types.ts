// Shared element-ref / children prop helpers, mirroring the shadcn-svelte
// convention so promoted components keep an identical prop API across consumers.

export type WithElementRef<T, El extends HTMLElement = HTMLElement> = T & {
	ref?: El | null;
};

export type WithoutChildren<T> = Omit<T, 'children'>;
export type WithoutChild<T> = Omit<T, 'child'>;
export type WithoutChildrenOrChild<T> = Omit<T, 'children' | 'child'>;
