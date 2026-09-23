# Contributing

LatticeShadow is a development prototype. Start with an issue that describes a
reproducible problem or a concrete improvement. Small changes that improve the
local save, recall, and deletion workflow are especially useful.

Use a branch prefixed `codex/` for agent work. Run `make setup` once on macOS or
`make setup-db` for DB-only work. Before opening a pull request, run the relevant
checks: `make test-db`, `make test-cli` on macOS, and `make docs`. CI checks
documentation on Linux and selects macOS checks from the changed paths. Model,
native companion, and app bundle checks run when their inputs change. Hardware
tests are opt-in.

Treat the GUI, CLI help, and user manual as one product surface. When an action,
label, default, error, or recovery step changes, update all affected surfaces in
the same pull request. Run `make docs` to check links, examples, and the
documented core actions; use a disposable macOS profile to check interactive
behavior. The manual should explain limits as carefully as successful paths.

Keep the DB independently installable, keep macOS dependencies in the CLI, and
use disposable test data. Do not commit personal memory databases, generated
benchmarks, credentials, or local environments. Describe experimental behavior
accurately and include the workload behind performance claims.

Security issues should follow [SECURITY.md](SECURITY.md), not a public issue.
