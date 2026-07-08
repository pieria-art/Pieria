# CLAUDE.md — Screen-Docent

> Claude Code reads this automatically at session start.
> This is a thin adapter. The real context lives in `.ai/`.

## Step 1: Read the control files (mandatory, every session)

Before writing a single line of code, read these in order:

1. `.ai/active_context.md` — current phase, state, immediate next steps (wins if files conflict).
2. `.ai/system_architecture.md` — the technical blueprint; align all solutions here.
3. `.ai/decision_log.md` — decisions are numbered **ADR-###**. Never re-decide without flagging.

## Step 2: Operating doctrine (same as all projects)

- **Sonnet** — all build work. **Opus** — planning, architecture (`/model`).
- **Plan mode** (Shift+Tab) before any non-trivial implementation.
- `/clear` between unrelated tasks. `/effort low` for simple mechanical tasks.
- Never hardcode API keys or credentials. No secrets in this repo (gitleaks pre-commit enforces).

## Step 3: Project-specific context

Screen-Docent is a **Raspberry Pi appliance** (`docent-living-room`, all-in-one) — a Flask app with
**Alembic**-managed **SQLite**, served in Docker on the Pi. Build model: **author-here / execute-on-Pi**.

- **Deploy = git + docker on the Pi.** `git reset --hard origin/main` then an appliance compose build.
  Pi Docker needs **`sudo` in a real terminal** (no passwordless; the `!`-prefix path can't allocate a
  TTY for the sudo prompt) — Josh runs that line. Boot runs `db_migrate.run_migrations()` (ADR-035):
  migration failure **halts boot**, caught at deploy, not by a black screen.
- **Schema = Alembic single source of truth** (ADR-035). `create_all` is gone from boot; the squashed
  baseline is `migrations/versions/0001_baseline.py`. Don't reintroduce `create_all`.

### Lab relationship (this is the Phase 3 gateway pilot)
- Screen-Docent is the **Phase 3 pilot for routing a cloud key (Gemini) through the LiteLLM gateway**
  (INFRA-D012): app → `GATEWAY_HOST_REDACTED:4000` with a **scoped virtual key**, not the provider
  directly. Migrating its Gemini calls behind the gateway is the pilot's goal (needs the Pi pointed at
  lab DNS `.109` to resolve the name; else the gateway IP works as a stopgap).
- The lab-wide standard is `~/ai-workspace/infrastructure/reference/lab-conventions.md`. **Note:** the
  Teleport `lab-ssh` host-access model there is for MS-01 lab nodes — it does **not** apply to this Pi
  (Screen-Docent is not a Teleport node; work happens on the Pi directly).

## Wrap-Up Protocol
1. Update `.ai/active_context.md` to reflect the new state.
2. Append any major decision to `.ai/decision_log.md` (next `ADR-###`).
