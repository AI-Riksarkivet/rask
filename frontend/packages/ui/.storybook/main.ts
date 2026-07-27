import type { StorybookConfig } from '@storybook/svelte-vite';

// @rask/ui is a Svelte component *library* (svelte-package), not a SvelteKit app —
// so the framework is @storybook/svelte-vite, not @storybook/sveltekit.
const config: StorybookConfig = {
	stories: ['../src/**/*.stories.@(js|ts|svelte)'],
	addons: ['@storybook/addon-docs', '@storybook/addon-svelte-csf', '@storybook/addon-a11y'],
	framework: {
		name: '@storybook/svelte-vite',
		options: {},
	},
};

export default config;
