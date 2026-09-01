# Settings And Prompt Skills Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add runtime automation controls and managed coach/sleep prompt skills to the open-source web console, while exposing the same state as read-only information in the personal frontend.

**Architecture:** Store runtime overrides and prompt-skill metadata under ignored `data/` files using atomic writes. The open-source settings API requires a bearer admin token for every read or write that exposes prompt content; the personal API returns only public summaries and implements no mutation endpoint. Report generators resolve the active prompt at call time so changes apply without restarting the process.

**Tech Stack:** Python standard library HTTP server, atomic JSON/Markdown persistence, vanilla HTML/CSS/JavaScript, Discord.py commands, unittest.

---

### Task 1: Runtime settings and managed skill store

**Files:**
- Create: `src/runtime/runtime_settings.py`
- Create: `src/runtime/prompt_skills.py`
- Test: `tests/test_runtime_settings.py`
- Test: `tests/test_prompt_skills.py`

1. Write tests for strict booleans, environment fallbacks, persisted overrides, Markdown size/type validation, activation, and reset.
2. Run the tests and confirm they fail before implementation.
3. Implement locked atomic persistence beneath `data/` with optional test-path environment overrides.
4. Run the tests and confirm they pass.

### Task 2: Resolve prompts and automation flags at runtime

**Files:**
- Modify: `agents/coros_report/agent.py`
- Modify: `agents/coros_report/auto_report.py`
- Modify: `agents/coros_report/sleep_report.py`
- Modify: `agents/coros_report/coros_capability.py`

1. Replace module-load prompt constants with call-time prompt resolution.
2. Make scheduled callbacks check persisted automation flags on every run.
3. Register both workout and sleep schedulers at startup so a live enable takes effect without restart.
4. Add administrator-only Discord text commands to inspect and change automation flags and managed skills.

### Task 3: Secure open-source settings API

**Files:**
- Modify: `src/api/web_server.py`
- Modify: `.env.example`

1. Add a bearer-token guard using constant-time comparison.
2. Add authenticated settings read and mutation actions with request-size limits and allowlisted fields.
3. Keep language local to the browser and never return the configured token.

### Task 4: Open-source settings interface

**Files:**
- Create: `web/settings.html`
- Create: `web/settings.js`
- Modify: `web/styles.css`
- Modify: `web/i18n.js`
- Modify: `web/index.html`
- Modify: `web/data.html`
- Modify: `web/tech.html`

1. Add navigation and automation toggles.
2. Add coach/sleep segmented views with Markdown paste, `.md` upload, preview, save, activate, and reset.
3. Store the admin token only in session storage.
4. Verify desktop and mobile layouts with browser screenshots.

### Task 5: Personal read-only settings interface

**Files:**
- Create: `web/settings.html`
- Create: `web/settings.js`
- Modify: `web/styles.css`
- Modify: `web/index.html`
- Modify: `web/data.html`
- Modify: `web/tech.html`
- Modify: `src/api/web_server.py`

1. Add a public summary endpoint containing automation state and non-sensitive skill metadata only.
2. Add a settings page with status rows and no form controls or mutation requests.
3. Verify the endpoint rejects settings POST requests.

### Task 6: Verification and documentation

**Files:**
- Modify: `README.md`

1. Run unit tests, `compileall`, and JavaScript syntax checks.
2. Start both web servers and verify settings pages at desktop and mobile widths.
3. Document configuration, security boundaries, and Discord commands.
