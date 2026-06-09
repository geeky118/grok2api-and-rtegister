# System Overview

## Purpose

This repository is a deployable Python-based Grok service stack. It combines an OpenAI-compatible `grok2api` API gateway, a registration task console, browser automation runtime dependencies, WARP/FlareSolverr proxy helpers, and deployment documentation.

## Main Components

- `grok2api-python-stack/vendor/grok2api/`: upstream FastAPI gateway. It exposes OpenAI-compatible chat, responses, image, file, admin, and function routes. Key paths include `main.py`, `app/api/v1/`, `app/services/grok/`, `app/services/reverse/`, `app/services/token/`, and `config.defaults.toml`.
- `grok2api-python-stack/apps/console/`: stack-integrated `grok-register` web console. It creates and tracks registration tasks, stores task runtime files, and pushes registered tokens into the `grok2api` admin token endpoint.
- `grok-register/`: standalone copy of the registration console. Treat it as a separate deployable surface unless a change must be mirrored into the stack-integrated console.
- `grok2api-python-stack/docker-compose.yml`: local/stack compose entrypoint for `warp`, `flaresolverr`, `grok2api`, and `console`.
- `grok2api-python-stack/deploy/`: deployment compose and helper scripts for server-oriented operation.
- `grok2api-python-stack/apps/worker-runtime/`: Docker image for browser registration automation with Chrome, Xvfb, DrissionPage, and console dependencies.
- `grok2api-python-stack/runtime/` and `grok2api-python-stack/apps/console/runtime/`: generated runtime data. These directories contain state, logs, task artifacts, and should not be committed.

## Boundaries and Contracts

- Public API contract: `grok2api` listens on container port `8000` and provides `/health`, `/v1/chat/completions`, `/v1/responses`, `/v1/images/*`, `/v1/admin/*`, and function UI/API routes.
- Admin/token contract: the registration console pushes successful tokens to `GROK_REGISTER_DEFAULT_API_ENDPOINT`, normally `http://grok2api:8000/v1/admin/tokens` inside the compose network, using `GROK_REGISTER_DEFAULT_API_TOKEN`.
- Proxy contract: registration and Grok upstream traffic normally use `socks5://warp:1080`; Cloudflare clearance refresh uses `FLARESOLVERR_URL`, normally `http://flaresolverr:8191`.
- Configuration contract: `.env.example` documents deploy-time env variables. Real `.env`, `config.toml`, app keys, API keys, mail credentials, SSH values, logs, SQLite files, and browser profiles must stay outside git.
- Runtime persistence contract: `grok2api` stores data/logs under `grok2api-python-stack/runtime/grok2api/`; console tasks are under `grok2api-python-stack/apps/console/runtime/tasks/`.
- Vendor boundary: changes under `vendor/grok2api` alter the API gateway. Stack-level overrides, compose wiring, and registration runtime behavior live outside that vendor boundary.

## Risky Areas

- Grok app-chat anti-bot behavior changes frequently. Before editing app-chat, image, reverse, or clearance logic, read `DEPLOY.md` and `grok2api-python-stack/docs/grok-app-chat-antibot-runbook.md`.
- Image and function imagine flows have separate paths. `/v1/images/generations` and `/v1/function/imagine/ws` or `/v1/function/imagine/sse` can fail for different reasons.
- Browser automation in Docker depends on Chrome, Xvfb, process cleanup, proxy format, and Turnstile behavior. Avoid changing lifecycle code without checking task logs and zombie-process risk.
- Token quota/account pool behavior affects availability and cost. Changes in `app/services/token/` or quota constants need targeted tests plus a live smoke when possible.
- Compose defaults can look correct locally but fail on a server if runtime data already contains older `config.toml`; env values are not always reapplied after first initialization.

## Notes for Agents

- Keep deployable defaults secret-free. Use placeholders in docs and examples.
- Prefer Docker verification for stack-level changes because the console and registration runtime depend on OS/browser packages.
- Use the cheapest meaningful check first: syntax compile, unit tests, compose config, container health, then API smoke.
- When modifying both standalone `grok-register/` and stack-integrated `apps/console/`, state why both surfaces need the same change.
- For user-facing pages, remove placeholder/debug/prototype copy before finishing unless the user explicitly requested a prototype.
