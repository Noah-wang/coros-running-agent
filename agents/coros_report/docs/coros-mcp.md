# COROS MCP Overview

## What It Is

COROS MCP is the official Model Context Protocol interface for COROS training data.
It lets an AI agent read authorized COROS data through structured tools instead of
calling private app APIs directly.

In this project, COROS MCP is used as the data layer for the Coros Report Agent.

```text
Discord message
  -> Coros Report Agent
  -> COROS MCP tools
  -> COROS training data
  -> LLM analysis
  -> Discord report
```

## Why Use MCP

MCP gives the agent a controlled tool interface:

- The agent can discover what COROS tools are available.
- Each tool has a name, description, and input schema.
- The agent calls specific tools instead of freely accessing arbitrary APIs.
- COROS handles authorization and exposes only supported data.

This is safer and easier to maintain than giving the agent raw API access.

## Current MCP Endpoint

The working endpoint for this project is:

```text
https://mcpus.coros.com/mcp
```

The generic public endpoint may redirect or authorize against a regional resource.
Using the regional URL avoids the protected-resource mismatch seen during OAuth.

## Main Tool Categories

### Activity Data

These tools provide workout history and detailed activity data.

```text
querySportRecords
getActivityDetail
queryActivityLapData
queryCustomActivityLapData
downloadActivityFitFiles
queryActivityFitFileDownloadUrls
```

Typical use:

```text
1. Call querySportRecords to find recent workouts.
2. Use labelId and sportType from the result.
3. Call getActivityDetail for full workout metrics.
4. Call queryActivityLapData for lap or segment breakdown.
```

### Training Status

These tools provide training load, recovery, fitness assessment, and schedule data.

```text
queryTrainingLoadAssessment
queryRecoveryStatus
queryFitnessAssessmentOverview
queryTrainingSchedule
```

Typical use:

```text
Use these after reading activity data to decide whether the next workout should be
easy, moderate, intense, or rest-focused.
```

### Health Data

These tools provide daily wellness signals.

```text
queryDailyHealthData
querySleepData
querySleepHrv
queryAvgHeartRate
queryRestingHeartRate
queryStressLevel
queryStressTimeSeries
queryHealthCheckTimeSeries
```

Typical use:

```text
Use health data to explain recovery, fatigue, sleep quality, stress, and readiness.
Do not invent missing metrics.
```

### Profile And Devices

These tools provide account profile and device information.

```text
queryUserInfo
queryDevices
```

Typical use:

```text
Use profile data only when the user asks about body metrics, age, height, weight,
or device information.
```

### Other Data

```text
queryMenstruationCycles
```

Use only when the user explicitly asks about menstrual cycle status or related notes.

## Recommended Report Flow

For a normal post-workout report:

```text
1. querySportRecords
   Find the latest relevant workout.

2. getActivityDetail
   Read distance, time, pace or speed, heart rate, cadence, elevation, calories,
   and training effect when available.

3. queryActivityLapData
   Read lap or segment data for pacing and effort analysis.

4. queryRecoveryStatus
   Check current recovery percentage and estimated full recovery time.

5. queryTrainingLoadAssessment
   Check whether recent load is optimized, too high, or too low.

6. Generate the report
   Summarize the workout, explain performance, identify risks, and recommend the
   next session.
```

## Report Rules

The agent should follow these rules:

- Use only data returned by COROS MCP.
- Do not invent heart rate, pace, calories, route, elevation, HRV, or training load.
- If a metric is missing, say it is unavailable.
- Explain the meaning of the data in plain language.
- Avoid medical diagnosis.
- Recommend rest or easy training when recovery or load data suggests fatigue.

## What This MCP Does Not Mean

The listed tools are the current tools exposed by COROS MCP to this authenticated user.
They are not guaranteed to represent every internal data field COROS stores.

In short:

```text
COROS MCP tools = data currently available to this agent
COROS internal data = broader private system, not fully exposed here
```

## Current Project Usage

This project connects to COROS MCP from:

```text
src/coros_mcp.py
```

The report agent uses it from:

```text
src/agent.py
```

The Discord interaction entry is:

```text
src/discord_bot.py
```
