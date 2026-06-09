# Harness Initialization

## Goal

Initialize repository-local harness documentation so future agents can quickly understand the project, execution rules, architecture boundaries, and verification paths.

## Context

- Root docs before this task: `README.md` and `DEPLOY.md`.
- Main stack path: `grok2api-python-stack/`.
- Standalone registration console path: `grok-register/`.
- Harness scaffold generated `AGENTS.md`, `docs/architecture/`, `docs/runbooks/`, and `docs/exec-plans/`.

## Constraints

- Keep `AGENTS.md` short and link outward.
- Do not introduce secrets or live server credentials.
- Preserve existing project docs instead of rewriting deployment history.

## Approach

Use the `harness-engineering` scaffold, then replace generic placeholders with project-specific commands, boundaries, invariants, risk areas, and verification steps discovered from the existing README, deploy notes, compose files, env example, requirements files, and vendor metadata.

## Steps

1. Run `init_harness_project.py` against the repository root.
2. Update `AGENTS.md` as the short operator entrypoint.
3. Update `docs/architecture/system-overview.md` with project components and contracts.
4. Update `docs/runbooks/verification.md` with practical checks.
5. Move this plan directly to completed after verification.

## Validation

- Confirm generated docs contain no unresolved placeholder markers.
- Inspect git status to verify the intended doc files are the only changed files.

## Decisions

- Keep deployment detail in `DEPLOY.md`; harness docs link to it instead of duplicating the operational log.
- Document both `grok2api-python-stack/apps/console/` and `grok-register/` because they are separate surfaces that may need mirrored changes.

## Risks

- Some verification commands depend on local Python packages, Docker, live API keys, proxy availability, or upstream Grok behavior. The runbook records those prerequisites instead of assuming they are always available.

## Open Questions

- None for initial harness bootstrap.
