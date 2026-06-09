# Verification Runbook

Use the smallest check that proves the changed surface works. Record skipped checks and the reason in the final response or in the active execution plan.

## Fast Checks

- Python syntax check for console/registration code:
  ```powershell
  python -m py_compile grok2api-python-stack\apps\console\app.py grok-register\app.py
  ```
- Vendor test subset:
  ```powershell
  cd grok2api-python-stack\vendor\grok2api
  python -m pytest tests
  ```
- Vendor lint:
  ```powershell
  cd grok2api-python-stack\vendor\grok2api
  python -m ruff check .
  ```
- Compose config render:
  ```powershell
  cd grok2api-python-stack
  docker compose --env-file .env.example config
  ```
- Stack build/start:
  ```powershell
  cd grok2api-python-stack
  docker compose --env-file .env up -d --build
  docker compose --env-file .env ps
  ```

## Task-Specific Checks

- API gateway health:
  ```powershell
  Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health
  ```
- Admin page reachability:
  ```powershell
  Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/admin/login
  ```
- Registration console reachability:
  ```powershell
  Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18600
  ```
- Registration task API smoke:
  ```powershell
  Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18600/api/tasks
  ```
- Container logs after runtime changes:
  ```powershell
  cd grok2api-python-stack
  docker compose --env-file .env logs --tail=200 grok2api console
  ```
- App-chat or image changes need a real API smoke with a valid API key and current proxy node. Keep secrets out of the command history/docs when recording evidence.
- Browser registration changes need a `count=1` task before any batch run. Confirm email retrieval, Turnstile handling, proxy use, token push, and process cleanup.

## Evidence

Capture concrete evidence such as command output, HTTP status codes, task IDs, screenshots, generated artifacts, or log snippets. If a check depends on live secrets, a server, or paid/upstream availability and cannot run locally, record the exact command and missing prerequisite.
