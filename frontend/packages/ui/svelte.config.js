import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

// The preprocessor for this package's own tools (svelte-check, vitest, Storybook). The console compiles
// the package's source with its own config.
export default {
	preprocess: vitePreprocess(),
};
