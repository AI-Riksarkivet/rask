.PHONY: help install build test lint fmt clean storybook

help:
	@echo "Targets: install build test lint fmt clean storybook"

install:
	bun install
	uv sync

build:
	cargo build --workspace
	uv sync
	bun run build

test:
	cargo test --workspace
	uv run pytest
	bun run test

lint:
	cargo clippy --workspace -- -D warnings
	uv run ruff check .
	bun run lint

fmt:
	cargo fmt --all
	uv run ruff format .
	bun run --filter '*' format

storybook:
	bun run storybook

clean:
	cargo clean
	rm -rf .venv node_modules **/node_modules **/dist **/.svelte-kit **/storybook-static
