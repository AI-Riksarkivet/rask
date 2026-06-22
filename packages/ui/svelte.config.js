import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

// @sveltejs/package 2.x removed the `package` config key (the files/exports
// filters). What ships is now controlled by package.json `files` (which excludes
// *.stories.* / *.test.*) and the explicit `exports` map. See kit discussions/8825.
export default {
	preprocess: vitePreprocess(),
};
