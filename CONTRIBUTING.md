# Contributing

LatticeShadow is a development prototype. Start with an issue that describes a
reproducible problem or a concrete improvement. Small changes that improve the
local save, recall, and deletion workflow are especially useful.

Use a branch prefixed `codex/` for agent work. Run `make setup` once on macOS or
`make setup-db` for DB-only work. Before opening a pull request, run the relevant
checks: `make test-db`, `make test-cli` on macOS, and `make docs`. CI checks
documentation on Linux. Changes beyond Markdown also run the CLI model check
and native companion build on macOS. Hardware tests are opt-in.

Keep the DB independently installable, keep macOS dependencies in the CLI, and
use disposable test data. Do not commit personal memory databases, generated
benchmarks, credentials, or local environments. Describe experimental behavior
accurately and include the workload behind performance claims.

Security issues should follow [SECURITY.md](SECURITY.md), not a public issue.
