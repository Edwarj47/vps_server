#!/usr/bin/env bash
set -euo pipefail

: "${DISCORD_APPLICATION_ID:?Set DISCORD_APPLICATION_ID}"
: "${DISCORD_GUILD_ID:?Set DISCORD_GUILD_ID}"
: "${DISCORD_BOT_TOKEN:?Set DISCORD_BOT_TOKEN}"

curl -fsS \
  -X PUT \
  "https://discord.com/api/v10/applications/${DISCORD_APPLICATION_ID}/guilds/${DISCORD_GUILD_ID}/commands" \
  -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary @- <<'JSON'
[
  {
    "name": "status",
    "description": "Check the local agent stack health",
    "type": 1
  },
  {
    "name": "ask",
    "description": "Ask the local Ollama agent",
    "type": 1,
    "options": [
      {
        "name": "prompt",
        "description": "What you want to ask",
        "type": 3,
        "required": true
      }
    ]
  },
  {
    "name": "research",
    "description": "Research current web information with cited sources",
    "type": 1,
    "options": [
      {
        "name": "query",
        "description": "What to research",
        "type": 3,
        "required": true
      }
    ]
  },
  {
    "name": "newsflash",
    "description": "Summarize latest tech and AI news",
    "type": 1
  },
  {
    "name": "memory",
    "description": "Search your agent memory",
    "type": 1,
    "options": [
      {
        "name": "query",
        "description": "Text to search for",
        "type": 3,
        "required": true
      }
    ]
  },
  {
    "name": "task",
    "description": "Queue a safe agent task",
    "type": 1,
    "options": [
      {
        "name": "task",
        "description": "Task to queue",
        "type": 3,
        "required": true
      }
    ]
  },
  {
    "name": "codex",
    "description": "Queue a Codex VPS job",
    "type": 1,
    "options": [
      {
        "name": "request",
        "description": "What Codex should work on",
        "type": 3,
        "required": true
      }
    ]
  },
  {
    "name": "approve",
    "description": "Approve or deny a pending agent job",
    "type": 1,
    "options": [
      {
        "name": "job_id",
        "description": "Agent job number",
        "type": 4,
        "required": true
      },
      {
        "name": "decision",
        "description": "Approve or deny the job",
        "type": 3,
        "required": true,
        "choices": [
          {
            "name": "approve",
            "value": "approve"
          },
          {
            "name": "deny",
            "value": "deny"
          }
        ]
      }
    ]
  }
]
JSON
