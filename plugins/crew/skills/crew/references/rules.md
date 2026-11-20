# Team Rules: `.crew/rules.json`

`crew init` writes a template. Fill it from the user's intake answers, then run `crew rules`: it validates the file, prints it in plain words and lists the tracked files that are fenced right now. Show that output to the user in the plan. Only the Lead writes this file.

```json
{
  "version": 1,
  "done": "Each issue has a PR whose proving test failed before and passes after, with the full suite green.",
  "never_touch": ["infra/", "src/auth/keys.py"],
  "allow_touch": [],
  "commands": {
    "setup": "npm ci",
    "suite": "npm test",
    "checks": { "typecheck": "npx tsc --noEmit", "lint": "npm run lint" }
  },
  "env": { "CI": "1" },
  "budget": { "hours": 8, "usd": 40, "deadline": "07:30" },
  "limits": { "max_builders": 4, "stuck_minutes": 20, "command_timeout_s": 1800 },
  "delivery": { "mode": "pr-per-issue", "base": "main", "branch": "" },
  "rates_usd_per_mtok": {}
}
```

## Fields

- `done`: the user's definition, in their words. Required.
- `commands.suite`: the full-suite command. Required. It must exit 0 on the untouched base, which `crew baseline` proves.
- `commands.setup`: run in every fresh checkout before any test (`npm ci`, `uv sync --frozen`, `bundle install`). Leave empty if a bare checkout can run the tests. Every proof pays for it once, so prefer the cached or frozen form.
- `commands.checks`: extra gates by name, each a command that must exit 0: type check, lint, build.
- `env`: variables set for every command the harness runs. `CI=1` stops most runners from opening a watch mode.
- `never_touch`: path globs no agent may write. Added to the built-in fence.
- `allow_touch`: path globs the user has explicitly said yes to. This is the only way to lift a fence. Never add to it on your own judgement.
- `budget.hours`, `budget.usd`: `crew budget` exits 3 when either is exceeded. Time is measured. Dollars need `rates_usd_per_mtok`.
- `budget.deadline`: `HH:MM` in the machine's local time (cloud machines are usually UTC, so convert), or a full ISO timestamp with an offset.
- `limits.max_builders`: issues built in parallel per wave. `stuck_minutes`: how long an agent may go without progress before it must hand back one question. `command_timeout_s`: per command; a timeout is a failure.
- `delivery.mode`: `pr-per-issue` (a branch and a PR per issue, each cut from `delivery.base`) or `single-branch` (every issue lands on `delivery.branch` through `crew land`, one PR). Use `single-branch` when the host session may push to only one branch.
- `rates_usd_per_mtok`: optional prices per million tokens, keyed by a fragment of the model name: `{"sonnet": {"in": 3, "out": 15, "cache_read": 0.3, "cache_write": 3.75}}`. Ask the user for current prices; do not fill these from memory. Without them the brief reports measured tokens and says the cost is not priced.

## Globs

Globs follow `.gitignore` rules. A pattern with no slash matches at any depth (`*.pem`, `migrations/`). A pattern with a slash is anchored at the repository root (`src/api/**`). A match on a directory covers everything under it.

## The built-in fence

Secrets may be neither read nor written: `.env`, `.env.*` (but not `.env.example`, `.env.sample`, `.env.template`, `.env.dist`), `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, SSH private keys, `.npmrc`, `.pypirc`, `.netrc`, `credentials.json`, `secrets.*`, `secrets/`.

These may be read but not changed: `migrations/`, `alembic/versions/`, `db/migrate/`, `billing/`, `payments/`.

The guard hook also refuses force pushes, hook-skipping commits, and anything that publishes or deploys (`npm publish`, `docker push`, `terraform apply`, `kubectl apply`, `gh release create`, and the like). The guard is prevention; `crew prove` is the exact check and fails any diff that touches a fenced path.

If an issue cannot be done without a fenced path, park it with one question. If the user says yes, add the narrowest glob that covers it to `allow_touch` and note the yes in `.crew/NOTES.md`.

## Commands for common stacks

| Stack | setup | suite | checks |
|---|---|---|---|
| Node | `npm ci` | `npm test` | `npx tsc --noEmit`, `npm run lint` |
| Python (uv) | `uv sync --frozen` | `uv run pytest -q` | `uv run ruff check .`, `uv run pyright` |
| Python (pip) | `pip install -e .[dev]` | `python -m pytest -q` | `ruff check .`, `mypy .` |
| Go | none | `go test ./...` | `go vet ./...` |
| Rust | none | `cargo test` | `cargo clippy -- -D warnings` |
| Ruby | `bundle install` | `bundle exec rspec` | `bundle exec rubocop` |

Take the real commands from the repository's CI file, not from this table.
