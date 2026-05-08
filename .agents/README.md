# Agent Skills

This directory defines reusable agent skills for the FX Engine project.
Each skill in `skills/` is a focused prompt that an AI agent can be given
to perform a specific task autonomously.

## Available Skills

| Skill | Purpose |
|-------|---------|
| `add-currency-pair.md` | Add a new currency to the engine |
| `add-endpoint.md` | Scaffold a new API endpoint following project conventions |
| `debug-test-failure.md` | Diagnose and fix a failing test |
| `security-review.md` | Review a changeset for financial/security issues |
| `rate-source-swap.md` | Replace the upstream rate provider |
| `performance-profile.md` | Identify and fix bottlenecks in the execute path |

## Usage

Give the skill file contents to any Claude/AI agent as its system prompt,
then provide the specific task as the user message.
