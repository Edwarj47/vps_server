# DCSS Agent Project Memory

This file contains curated, non-secret project facts for the read-only
MemPalace trial. Keep entries short so Discord memory search returns clean
answers instead of large operations-manual fragments.

## Current chat model

The production Discord `/ask` path currently uses
`qwen3:4b-instruct-2507-q4_K_M`. It replaced `llama3.2:3b` after local
benchmarks showed better memory follow-up, current-info guardrails, source
synthesis, tool routing, and prompt-injection resistance while staying within
safe VPS memory headroom.

## Codex authority model

Codex work can only enter through the explicit Discord `/codex` route. Ollama
may answer, summarize, search memory, recommend allowed `/task` or `/research`
paths, and explain next steps, but it must not create, request, imply, or
delegate Codex/VPS execution.

## News Flash

`/newsflash` summarizes recent technology and AI feed items from configured
RSS/Atom sources. The first source set includes Hacker News, OpenAI, Anthropic,
Google AI, Meta AI, and Mistral feeds. It is deterministic and does not send
feed text through Ollama.

## Task support

`/task` records a queued job in `agent_jobs`. The Phase 2 worker can execute
allowed internal tools such as health checks, constrained web research, News
Flash, memory search, and memory notes. It must not run shell, SSH, sudo,
Docker, service restarts, file edits, or Codex-like requests.

## Schedule support

`/schedule` manages future `/task` jobs. It supports listing scheduled jobs,
canceling jobs, and rescheduling jobs. Future work should include clearer
one-off versus recurring schedule UX before treating it as a full cron system.

## MemPalace production gate

MemPalace is currently a read-only trial, not automatic `/ask` memory. Promotion
requires reliable project-decision retrieval, clean user-history retrieval,
redacted or harmless secret/prompt-injection searches, better results than
Postgres substring search on the test set, and acceptable Discord latency.

## MemPalace safety model

The production agent must not use the stock MemPalace MCP server directly. The
local wrapper only exposes status, namespace listing, and search. Writes,
deletes, hooks, and retention actions stay outside the agent-facing path and
require admin approval.

## Codex handoff files

Approved `/codex` jobs create JSON handoff files under `codex-jobs/approved`.
The handoff records requester, approver, prompt, requested working directory,
allowed working directories, session mode, and execution policy. It does not
start a shell, SSH session, or Codex process.

## Codex allowed directories

Codex handoff jobs are constrained to explicit allowed work directories. The
current default is `/opt/dcss-n8n`, matching the tracked VPS automation stack.
Other directories should be added only when there is a specific job need.

## Secret handling

The memory pipeline redacts likely secrets before sanitized imports. Discord
memory search must not reveal real API keys, bot tokens, webhook shared
secrets, environment values, or credentials. Secret-oriented queries should
return redacted or harmless operational guidance, not secret values.
