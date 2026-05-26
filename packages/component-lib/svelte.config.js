import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

export default {
	preprocess: vitePreprocess(),
	package: {
		files: (filepath) => !/\.(test|spec|stories)\./.test(filepath),
		exports: (filepath) =>
			/^(index|components\/[^/]+\/index|utils\/index)\.(ts|svelte)$/.test(filepath),
	},
};
