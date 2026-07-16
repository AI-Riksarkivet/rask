import path from 'node:path';
import { includeIgnoreFile } from '@eslint/compat';
import prettier from 'eslint-config-prettier';
import svelte from 'eslint-plugin-svelte';
import { defineConfig } from 'eslint/config';
import globals from 'globals';
import ts from 'typescript-eslint';
import computeSvelteConfig from './components/frontends/compute/svelte.config.js';
import discoverSvelteConfig from './components/frontends/discover/svelte.config.js';
import frontendSvelteConfig from './components/frontends/home/svelte.config.js';
import overviewSvelteConfig from './components/frontends/overview/svelte.config.js';
import storageSvelteConfig from './components/frontends/storage/svelte.config.js';
import studioSvelteConfig from './components/frontends/studio/svelte.config.js';
import trainSvelteConfig from './components/frontends/train/svelte.config.js';
import libSvelteConfig from './packages/ui/svelte.config.js';
// Local cross-zone-reload rule lives in its own module so its zone-matching logic is
// unit-tested (eslint-rules/cross-zone-reload.test.js) — the regex silently going stale
// once already disabled the guard unnoticed.
import { raLocal } from './eslint-rules/cross-zone-reload.js';

const gitignorePath = path.resolve(import.meta.dirname, '.gitignore');

export default defineConfig(
	includeIgnoreFile(gitignorePath),
	ts.configs.recommended,
	svelte.configs.recommended,
	prettier,
	svelte.configs.prettier,
	{
		languageOptions: {
			globals: { ...globals.browser, ...globals.node },
		},
		rules: {
			// typescript-eslint owns this; the lint rule throws false positives in TS projects.
			// https://typescript-eslint.io/troubleshooting/faqs/eslint/#i-get-errors-from-the-no-undef-rule
			'no-undef': 'off',
			'@typescript-eslint/no-explicit-any': 'warn',
			'@typescript-eslint/no-unused-vars': [
				'error',
				{
					argsIgnorePattern: '^_',
					varsIgnorePattern: '^_',
					caughtErrorsIgnorePattern: '^_',
				},
			],
			// Map/Set/URL in non-reactive contexts (workers, callbacks) are fine.
			'svelte/prefer-svelte-reactivity': 'off',
			// GATE: keyless {#each} silently re-renders/reorders DOM on mutation — a
			// class of bug recent audits caught by hand. Error, not warn, so CI fails.
			'svelte/require-each-key': 'error',
			// GATE: bind reactive state, never reassign it imperatively — catches the
			// "assigned but not reactive" footgun. Zero violations on current code.
			'svelte/no-reactive-reassign': 'error',
			// Not using shallow routing.
			'svelte/no-navigation-without-resolve': 'off',
		},
	},
	{
		files: ['**/*.svelte', '**/*.svelte.ts', '**/*.svelte.js'],
		rules: {
			// $effect() blocks are expressions, not assignments — expected Svelte 5 usage.
			'@typescript-eslint/no-unused-expressions': 'off',
		},
	},
	{
		files: ['components/frontends/home/**/*.svelte', 'components/frontends/home/**/*.svelte.ts'],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: frontendSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/frontends/storage/**/*.svelte',
			'components/frontends/storage/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: storageSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/frontends/compute/**/*.svelte',
			'components/frontends/compute/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: computeSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/frontends/discover/**/*.svelte',
			'components/frontends/discover/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: discoverSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/frontends/overview/**/*.svelte',
			'components/frontends/overview/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: overviewSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/frontends/studio/**/*.svelte',
			'components/frontends/studio/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: studioSvelteConfig,
			},
		},
	},
	{
		files: [
			'components/frontends/train/**/*.svelte',
			'components/frontends/train/**/*.svelte.ts',
		],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: trainSvelteConfig,
			},
		},
	},
	{
		files: ['packages/ui/**/*.svelte', 'packages/ui/**/*.svelte.ts'],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: ['.svelte'],
				parser: ts.parser,
				svelteConfig: libSvelteConfig,
			},
		},
	},
	{
		// GATE: cross-zone <a> links must hard-navigate (see ZONE_PATH rule above).
		files: ['components/frontends/**/*.svelte'],
		plugins: { 'ra-local': raLocal },
		rules: { 'ra-local/cross-zone-reload': 'error' },
	},
	{
		ignores: [
			'**/.svelte-kit/',
			'**/build/',
			'**/dist/',
			'**/storybook-static/',
			'packages/ui/.storybook/',
		],
	},
);
