<script lang="ts" module>
	import { tv, type VariantProps } from 'tailwind-variants';

	export const buttonVariants = tv({
		base: 'inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50',
		variants: {
			variant: {
				default: 'bg-primary text-primary-foreground hover:bg-primary/90',
				destructive: 'bg-destructive text-destructive-foreground hover:bg-destructive/90',
				outline: 'border border-input bg-background hover:bg-accent hover:text-accent-foreground',
				ghost: 'hover:bg-accent hover:text-accent-foreground',
				link: 'text-primary underline-offset-4 hover:underline',
			},
			size: {
				sm: 'h-8 px-3',
				md: 'h-10 px-4 py-2',
				lg: 'h-11 px-8',
				icon: 'h-10 w-10',
			},
		},
		defaultVariants: { variant: 'default', size: 'md' },
	});

	export type ButtonVariant = VariantProps<typeof buttonVariants>['variant'];
	export type ButtonSize = VariantProps<typeof buttonVariants>['size'];
</script>

<script lang="ts">
	import { Button as BitsButton, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	type Props = WithoutChild<BitsButton.RootProps> & {
		variant?: ButtonVariant;
		size?: ButtonSize;
	};

	let {
		class: className,
		variant = 'default',
		size = 'md',
		ref = $bindable(null),
		children,
		...rest
	}: Props = $props();
</script>

<BitsButton.Root bind:ref class={cn(buttonVariants({ variant, size }), className)} {...rest}>
	{@render children?.()}
</BitsButton.Root>
