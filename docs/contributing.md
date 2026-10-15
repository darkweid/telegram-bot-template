# Contributing

The day-to-day workflow: the checks a change has to pass, how to add a
migration, how dependencies move, and how commits and pull requests are
shaped. It applies to a bot started from this template as much as to changes
to the template itself. Open it before your first commit.

## Development

```bash
pre-commit install            # run the hooks on every commit
make help                     # every target with a one-line description
make run-dev                  # the dev stack: polling, autoreload
make lint                     # every pre-commit hook over every file
make test                     # unit tests, no Docker needed
```

`make run-dev` builds and starts the dev stack. The bot polls under
watchfiles, which restarts it on any `.py` change under `src`, `loggers` and
`models`; the worker restarts the same way, the scheduler does not
(`make restart s=scheduler` after changing a schedule).

`make lint` runs `pre-commit run --all-files`, the same hooks CI runs:

- `pyupgrade`, and the standard `pre-commit-hooks` (YAML, TOML and JSON
  syntax, trailing whitespace, line endings, merge conflict markers, private
  keys, large files, debug statements);
- `migration-heads`: the Alembic chain has exactly one head;
- `sort-requirements-in`: the `infra/requirements/*.in` files stay sorted;
- `check-docs`: links, anchors, paths and make targets named in the
  Markdown files exist, and every `.env.example` key has a row in
  [the configuration reference](configuration.md);
- `ruff`, `black`, `autoflake`;
- `mypy` in strict mode, tests and migrations excluded;
- `import-linter`: the layer contracts in `.importlinter`;
- `bandit` over `src/`, `scripts/` and `migrations/`;
- `shellcheck` on shell scripts, `actionlint` on the workflows.

`make lint-imports` runs the layer contracts alone. `core-is-independent`
keeps `src.core` from importing a domain package or the composition root;
`handlers-skip-repositories` keeps handlers away from repositories and
SQLAlchemy, so they reach data through use cases. A new domain package goes
into the first contract's `forbidden_modules`, and its handlers module into
the second one's `source_modules`.

How to run and write tests is in [the testing guide](testing.md).

## Migrations

```bash
make migration m="add notes table"
```

The target runs in the dev image as your user, so the revision file belongs
to you: it starts PostgreSQL, migrates it to head, and autogenerates a revision
from the difference between the models and the database. Read the revision
before committing it. Autogenerate misses renames (it sees a drop and an
add) and some type and server-default changes.

- A new model module is imported in `models/__init__.py`, or autogenerate
  does not see it and produces an empty revision
  (`tests/unit/test_models_registration.py`).
- The chain keeps exactly one head; the `migration-heads` hook fails
  otherwise. Two branches that each added a migration need a merge revision
  or a rebase of one of them.
- Never edit a migration that has been applied anywhere: write a new one.
- `make migrate` applies the migrations baked into the bot image; the
  prod-like stack does the same on every start through the one-shot
  `migrate` service.

## Dependencies

Direct dependencies live in `infra/requirements/*.in`; the `*.txt` files next
to them are lockfiles generated from them. Never edit a `.txt` by hand.

```bash
make req-compile      # recompile the lockfiles after editing a .in file
make req-upgrade      # recompile, bumping every pin to its newest allowed release
make req-sync-dev     # install the dev lockfile into the active environment
```

`make req-compile` runs `pip-compile` inside a `linux/amd64` Python container
for every name in `REQ_NAMES` (`base`, `dev`, `prod`, `security`, `ansible`),
so the lockfiles come out the same on whatever machine you run it.

Updates arrive as pull requests:

- Dependabot (`.github/dependabot.yml`) opens one grouped pull request a
  month for the Python packages in `infra/requirements` and one for GitHub
  Actions, both with the `chore` prefix.
- `.github/workflows/pre-commit-autoupdate.yml` does the same for the hook
  revisions once a month. A pull request opened with the workflow token
  starts no CI; set the `PRECOMMIT_BOT_TOKEN` secret (a fine-grained token
  with contents and pull-requests write) for CI to run on it.
- A Dependabot bump of a package `.pre-commit-config.yaml` also pins (`mypy`,
  `bandit`) leaves the hook behind. Run
  `python scripts/ops/sync_precommit_deps.py` on that pull request and commit
  the result; `tests/unit/scripts/ops/test_sync_precommit_deps.py` fails until
  you do.
- Container images (`python`, `postgres`, `redis`, `nginx`, `certbot`) move
  by hand.

## Commits and pull requests

- **Branches** are named `<type>/<kebab-slug>`: `feat/broadcast`,
  `fix/webhook-secret-check`.
- **Commits** follow Conventional Commits (`feat:`, `fix:`, `refactor:`,
  `test:`, `docs:`, `build:`, `ci:`, `chore:`), in English, with a short
  imperative subject. One logical change per commit; a refactor and a
  feature go in separate commits.
- **`main` takes squash merges only.** The pull request title becomes the
  commit subject, so it follows Conventional Commits too, and the description
  becomes the commit body: write it as history, what changed and why,
  including any configuration change.
- **The pull request template** (`.github/pull_request_template.md`) ends
  with a "Checks" section. Delete that section before merging, so it does not
  end up in the squash commit body.
- **A failing check** is fixed with a new commit on the branch. Never
  force-push a branch under review.
- **A security problem** goes to the maintainers privately, never into an
  issue or a pull request: see
  [reporting a vulnerability](../SECURITY.md#reporting-a-vulnerability).
- **A change to a setting** updates its section in `src/main/config.py`,
  `.env.example` and [the configuration reference](configuration.md) in the
  same commit, and adds the key to `KNOWN_KEYS` in
  `scripts/ops/check_env.py`, which reports a key it does not know;
  `check-docs` fails while the reference misses one.
