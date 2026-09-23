# Working on LatticeShadow

## Apply Occam's Razor

- Prefer the simplest design that satisfies demonstrated requirements.
- Reuse, consolidate, or delete before adding abstractions or dependencies.
- Keep the DB independently installable. macOS dependencies belong in the CLI package.
- Work on branches prefixed with `codex/`.

## Layout and validation

- `packages/db`: reusable `latticeshadow-db` Python package.
- `packages/cli`: `latticeshadow-cli`, macOS daemon, UI, MCP interface, and native companion.
- Run `make setup` once; it installs both packages on macOS and only the DB elsewhere.
- Run `make test-db`, `make test-cli` on macOS, and `make docs` for relevant changes.
- Treat the GUI, CLI, and user manual as one product surface. When behavior,
  labels, defaults, errors, or recovery steps change, update every affected
  surface and the relevant getting-started/capability pages in the same PR.
  Run `make docs` to catch stale command, control, link, and print-edition references.
- `make test` runs the standard suites. Hardware tests are explicitly selected with `make test-hardware`.
- Keep tests isolated from personal clipboard data, shell configuration, and live credentials.
- Installing development dependencies must not enable listeners or modify shell startup files.
- Generated databases, benchmark corpora, environments, and local credentials stay out of Git.

## Product direction

Read `docs/SPRINT.md` for the improvement backlog.
Describe experimental behavior accurately; hash embeddings are not semantic embeddings,
distance-preserving rotations are not opaque vector encryption, and simulated proofs are not zk-SNARKs.
