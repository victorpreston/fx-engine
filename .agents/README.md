# Agent Skills

This directory defines reusable agent skills for the FX Engine project.
Each skill in `skills/` is a focused prompt that an AI agent can be given
to perform a specific task autonomously.

## Available Skills

| Skill | Purpose |
|-------|---------|
| `add-currency-pair.md` | Add a new currency to the engine |
| `add-endpoint.md` | Scaffold a new API endpoint following project conventions |
| `add-migration.md` | Create a versioned Alembic migration with composite index guidelines |
| `add-customer-tier.md` | Implement per-customer spread tiers (standard / premium) |
| `add-background-worker.md` | Add a recurring async worker (quote expiry, outbox relay, etc.) |
| `debug-test-failure.md` | Diagnose and fix a failing test |
| `implement-outbox-pattern.md` | Guarantee event delivery via DB outbox + background relay |
| `implement-pagination.md` | Add cursor-based pagination to a list endpoint |
| `implement-reverse-quote.md` | Reverse quote: given to_amount, compute required from_amount |
| `rate-source-swap.md` | Replace the upstream rate provider |
| `security-review.md` | Review a changeset for financial and security issues |

## Usage

Give the skill file contents to any Claude/AI agent as its system prompt,
then provide the specific task as the user message.
