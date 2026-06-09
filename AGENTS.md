# AGENTS.md

## Purpose

This repository packages a deployable `grok2api` Python stack plus a `grok-register` task console. Keep durable operating knowledge in repo docs so future agents can work without reconstructing context from chat.

## Start Here

1. Read this file first.
2. Read [docs/architecture/system-overview.md](docs/architecture/system-overview.md).
3. For deploy or runtime questions, read [DEPLOY.md](DEPLOY.md) and [docs/runbooks/verification.md](docs/runbooks/verification.md).
4. For non-trivial work, create or update a plan in [docs/exec-plans/active/](docs/exec-plans/active/).
5. Run the smallest meaningful verification before finishing.

## Working Rules

- Keep this file short and link outward to deeper docs.
- Record recurring gotchas, commands, and invariants in the repo.
- Turn repeated review feedback into tests, schemas, or scripts.
- Say explicitly when verification could not run.
- Do not commit real secrets, runtime data, logs, browser profiles, SSH details, or live server credentials.
- Preserve the separation between the upstream `vendor/grok2api` service, stack-level deployment files, and the standalone `grok-register` copy unless the task explicitly spans them.
- External-facing UI pages must not include placeholder, template, implementation-note, debug, or prototype copy unless the user explicitly asks for a prototype.

## Commands

- Stack env template: `Copy-Item grok2api-python-stack\.env.example grok2api-python-stack\.env`
- Stack build/start: `cd grok2api-python-stack; docker compose --env-file .env up -d --build`
- Stack status: `cd grok2api-python-stack; docker compose --env-file .env ps`
- Stack logs: `cd grok2api-python-stack; docker compose --env-file .env logs --tail=200 grok2api console`
- Console local run: `cd grok2api-python-stack; python apps/console/app.py`
- Vendor tests: `cd grok2api-python-stack\vendor\grok2api; python -m pytest tests`
- Vendor lint: `cd grok2api-python-stack\vendor\grok2api; python -m ruff check .`

## Docs Index

- Architecture: [docs/architecture/system-overview.md](docs/architecture/system-overview.md)
- Verification: [docs/runbooks/verification.md](docs/runbooks/verification.md)
- Execution plans: [docs/exec-plans/](docs/exec-plans/)
- Deployment notes: [DEPLOY.md](DEPLOY.md)

## Definition of Done

- The requested change is implemented.
- Validation was run or a precise blocker is documented.
- New durable knowledge was written back into the repo if needed.
