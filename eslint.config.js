import path from 'node:path';
import { includeIgnoreFile } from '@eslint/compat';
import prettier from 'eslint-config-prettier';
import svelte from 'eslint-plugin-svelte';
import { defineConfig } from 'eslint/config';
import globals from 'globals';
import ts from 'typescript-eslint';
import computeSvelteConfig from './components/apps/compute-frontend/svelte.config.js';
import discoverSvelteConfig from './components/apps/discover-frontend/svelte.config.js';
import frontendSvelteConfig from './components/apps/frontend/svelte.config.js';
import overviewSvelteConfig from './components/apps/overview-frontend/svelte.config.js';
import storageSvelteConfig from './components/apps/storage-frontend/svelte.config.js';
import studioSvelteConfig from './components/apps/studio-frontend/svelte.config.js';
import trainSvelteConfig from './components/apps/train-frontend/svelte.config.js';
import libSvelteConfig from './packages/ui/svelte.config.js';

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
		files: ['components/apps/frontend/**/*.svelte', 'components/apps/frontend/**/*.svelte.ts'],
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
			'components/apps/storage-frontend/**/*.svelte',
			'components/apps/storage-frontend/**/*.svelte.ts',
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
			'components/apps/compute-frontend/**/*.svelte',
			'components/apps/compute-frontend/**/*.svelte.ts',
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
			'components/apps/discover-frontend/**/*.svelte',
			'components/apps/discover-frontend/**/*.svelte.ts',
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
			'components/apps/overview-frontend/**/*.svelte',
			'components/apps/overview-frontend/**/*.svelte.ts',
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
			'components/apps/studio-frontend/**/*.svelte',
			'components/apps/studio-frontend/**/*.svelte.ts',
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
			'components/apps/train-frontend/**/*.svelte',
			'components/apps/train-frontend/**/*.svelte.ts',
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
		ignores: [
			'**/.svelte-kit/',
			'**/build/',
			'**/dist/',
			'**/storybook-static/',
			'packages/ui/.storybook/',
		],
	},
);
