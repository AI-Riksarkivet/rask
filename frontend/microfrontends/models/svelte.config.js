import adapter from 'svelte-adapter-bun';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	compilerOptions: {
		experimental: { async: true },
	},
	kit: {
		adapter: adapter(),
		// The MODELS microfrontend — the model-lifecycle zone, served under /models. It
		// supersedes the `train` zone: training is one AREA of a model's life (submit,
		// runs, monitoring, analysis), beside the registry that lakehouse used to serve
		// at /lakehouse/models, the experiments and pipeline surfaces that came with it,
		// and the inference playground. One zone owns a model from training through
		// blessing to inference; nothing about a model is answered in two places.
		paths: { base: '/models' },
		experimental: { remoteFunctions: true },
	},
};

export default config;
