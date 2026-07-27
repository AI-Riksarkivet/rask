import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

// @sveltejs/package 2.x removed the `package` config key. What ships is controlled
// by package.json `files` (excludes *.stories.* / *.test.*) + the explicit `exports` map.
export default {
	preprocess: vitePreprocess(),
};
