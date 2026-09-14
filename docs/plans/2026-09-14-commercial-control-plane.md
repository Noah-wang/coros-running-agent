# Commercial Control Plane Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a backward-compatible multi-tenant control plane and protected operations dashboard to the running COROS Agent.

**Architecture:** Keep the existing Agent runtime as the default tenant while introducing a request-scoped tenant context, a small SQLite control-plane database, tenant-scoped memory/conversations/usage, and isolated `mcp-remote` OAuth directories. Extend the existing token-protected settings surface into an operator dashboard; do not expose secrets or personal workout data in admin APIs.

**Tech Stack:** Python 3.13, standard-library SQLite, `http.server`, vanilla HTML/CSS/JavaScript, pytest, existing Discord and MCP integrations.

**Implementation status:** Tasks 1-7 implemented and verified locally. Production rollout keeps multi-tenant routing disabled until the owner Discord identity is bound.

---

### Task 1: Tenant context and control-plane database

**Files:**
- Create: `src/runtime/tenant.py`
- Create: `src/runtime/control_store.py`
- Test: `tests/test_tenant_control_plane.py`

1. Write failing tests for context isolation, tenant CRUD, identity bindings, and subscription updates.
2. Run `uv run pytest tests/test_tenant_control_plane.py -q` and verify failure.
3. Implement request-scoped tenant context and SQLite schema with WAL, foreign keys, UUID public IDs, and audit timestamps.
4. Run the focused tests and commit.

### Task 2: Tenant-scoped state and usage

**Files:**
- Modify: `src/runtime/paths.py`
- Modify: `src/runtime/memory.py`
- Modify: `src/runtime/conversation.py`
- Modify: `src/runtime/usage_store.py`
- Modify: `src/runtime/trace.py`
- Test: `tests/test_tenant_state_isolation.py`

1. Write failing cross-tenant memory, conversation, and usage tests.
2. Preserve existing paths for the `default` tenant and route new tenants under `data/tenants/<tenant_id>/`.
3. Add tenant ID to every persisted usage event while retaining the old aggregate summary API.
4. Run focused and existing runtime tests and commit.

### Task 3: COROS OAuth isolation

**Files:**
- Modify: `src/integrations/coros_mcp.py`
- Modify: `agents/coros_report/auth_flow.py`
- Test: `tests/test_coros_tenant_auth.py`

1. Write failing tests asserting unique MCP config directories and callback ports per tenant.
2. Pass `MCP_REMOTE_CONFIG_DIR` to every `mcp-remote` subprocess.
3. Replace the single global auth process with a per-tenant process map.
4. Run focused COROS tests and commit.

### Task 4: Discord tenant routing

**Files:**
- Modify: `src/bot/discord_bot.py`
- Modify: `src/orchestrator.py`
- Test: `tests/test_discord_tenant_routing.py`

1. Write tests for known identity routing and unknown-user rejection when multi-tenant mode is enabled.
2. Resolve `message.author.id` through the control store and wrap each request in its tenant context.
3. Keep legacy default-tenant behavior behind `MULTI_TENANT_ENABLED=false` so the current bot is not disrupted.
4. Run Discord routing tests and commit.

### Task 5: Protected operator API

**Files:**
- Modify: `src/api/web_server.py`
- Test: `tests/test_admin_api.py`

1. Write tests for authorization, payload redaction, tenant creation, subscription changes, and identity binding.
2. Add `/api/admin` GET and action-based POST endpoints using the existing constant-time bearer-token check.
3. Return only provider configuration status; never return key values, OAuth tokens, prompts, conversations, or workout data.
4. Run API tests and commit.

### Task 6: Operations dashboard

**Files:**
- Create: `web/admin.html`
- Create: `web/admin.js`
- Modify: `web/styles.css`
- Modify: `web/settings.html`
- Test: `tests/test_admin_assets.py`

1. Add a restrained operations UI for overview, tenants, subscriptions, identities, provider status, and usage.
2. Reuse the existing admin token for the first private beta.
3. Verify keyboard navigation, mobile layout, empty/error/loading states, and safe DOM rendering.
4. Run asset tests and capture desktop/mobile screenshots.

### Task 7: Migration, documentation, and deployment

**Files:**
- Create: `scripts/bootstrap_default_tenant.py`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `docs/COMMERCIAL_CONTROL_PLANE.md`

1. Bootstrap the current installation as the `default` tenant without moving personal files.
2. Document tenant boundaries, backup requirements, staged rollout, and Feishu follow-up work.
3. Run `uv run pytest -q` and `uv run python -m compileall src agents`.
4. Back up the server source and control-plane database, deploy, restart only web/bot services, and run health checks.
