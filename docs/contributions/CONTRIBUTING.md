# Contributing

See `README.md` at the repo root for the build / test loop.

## Commit style

Conventional commits — see `cliff.toml` for the changelog parsers.

## Adding a UI component

The console's components come from `@riksarkivet/ui`: a new or changed component is made in the
[`AI-Riksarkivet/ui`](https://github.com/AI-Riksarkivet/ui) repository (its `README.md` and `DESIGN.md`), and reaches
the console when `frontend/console/package.json` pins the commit that carries it.
