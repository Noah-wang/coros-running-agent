# Architecture

This page describes the web-facing architecture of COROS Running Agent.

## 1. Main Agent Loop

The main agent is not a one-shot classifier. It runs a loop:

1. Receive a user request.
2. Choose a tool.
3. Observe the result.
4. Decide whether more context is needed.
5. Draft an answer.
6. Run a lightweight reflection check.
7. Answer, search again, or ask a follow-up question.

This matters because many running questions cannot be routed correctly before looking at data.

## 2. Capability Layer

Domain features are packaged as capabilities. A capability can expose read tools and text commands.

Read tools return structured data to the model. Text commands perform user-facing actions such as generating a report or listing activities.

The runtime discovers capabilities from the `agents/` directory, so adding a new capability does not require rewriting the main agent loop.

## 3. Read-Only Web Boundary

The public web console is intentionally read-only.

Write tools such as importing videos, logging feelings, and syncing FIT files are hidden from the web tool table. The model cannot call tools it cannot see.

Write actions should stay behind Discord, CLI, or another authenticated interface.

## 4. COROS MCP

COROS data is accessed through COROS MCP via `mcp-remote`.

The agent can list activities, fetch selected activity details, read PB-related data, and use that context to generate workout reports.

The MCP endpoint is shared, but authorization is user-specific.

## 5. Report Workflow

Workout reports use a graph-style workflow:

1. Plan which COROS tools are needed.
2. Fetch training data.
3. Generate a structured report.
4. Critique the draft.
5. Revise the answer before returning it.

This gives the report path a stable structure while still allowing the main agent to route natural-language requests.

## 6. RAG Knowledge Base

Books, subtitles, and notes are chunked, embedded, and retrieved as supporting evidence.

The model receives retrieved passages with source metadata, then answers with citations when the knowledge base is used.

## 7. Long-Term Memory

Long-term memory stores stable runner context such as PBs, goals, mileage, injuries, subjective workout feelings, and race history.

Short-lived UI state, such as the last displayed activity list, is kept separate from long-term memory so it does not pollute prompts.

## 8. Observation And Reflection

Tool results are fed back to the agent as observations. After a draft is produced, a reflection step checks whether the answer matches the request and whether a required data source was skipped.

If the answer is under-supported, the agent can run another tool call or ask the user for missing context.

## 9. Live Execution Trace

The web UI receives `trace_step` events over SSE. Each event maps a tool or workflow stage to a module in the visual trace.

The trace is observational only. It does not control routing, tool calls, or permissions.

## 10. Web Auto Report Notice

The web UI can poll a read-only endpoint for the latest COROS activity summary.

The endpoint only returns a short activity notice. It does not generate a full report, update long-term memory, or mark the activity as reported. The browser remembers dismissed activity IDs in local storage, so the same workout does not keep popping up for the same visitor.

When the user clicks **Interpret**, the web console opens a chat session and sends a normal workout-report prompt through the existing agent path.

## 11. Privacy Boundary

The repository contains runtime code, templates, and sample configuration. Local deployment data should not be committed:

- `.env`
- `agents/coros_report/config.toml`
- `data/`
- imported PDFs and subtitles
- embeddings, FIT files, route maps, and conversation logs
- Discord tokens, model keys, Bilibili cookies, and COROS auth state
