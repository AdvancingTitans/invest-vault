---
name: agent-reach
description: Invest Vault bundled read-only internet fallback for evidence gaps. Uses Agent Reach routing and never replaces deterministic market or accounting data with search snippets.
metadata:
  upstream: https://github.com/Panniantong/Agent-Reach
  version: "1.5.0"
---

# Agent Reach — Invest Vault evidence fallback

Use this Skill in every Invest Vault research-assistant and committee turn, but only
after the application-provided evidence pack has identified a concrete missing item.
It is a read-only supplement, not a replacement for `stock-analysis` or the Vault.

## Required routing

1. Run `agent-reach doctor --json` before using a multi-backend platform. Respect the
   reported `active_backend`; never claim an unavailable or unconfigured channel was
   searched.
2. Prefer primary sources: exchange/company/fund-manager/regulator documents, then
   reputable secondary sources. Use [references/search.md](references/search.md) for
   discovery and [references/web.md](references/web.md) for reading a known URL.
3. Keep title, URL, publisher, publication time and retrieval time for every result.
4. Search-result snippets, headlines and social posts are leads only. They may be
   reported as conditional evidence, never as verified original text.
5. Do not use web material to synthesize real-time auction/order-book figures,
   undisclosed fund holdings or subscriptions/redemptions, historical depth, credit
   spreads, liquidation value, management intent, culture or moat facts.
6. Do not read or change local ledgers, profiles, notes or credentials. Do not write
   files, post content, authenticate an account or alter browser state.
7. If a channel is unavailable or the original document cannot be read, return the
   precise remaining gap. Never fill it with a nearby metric or an estimate.

## Invest Vault evidence levels

- `verified_original`: the original page/document was read and its date/publisher match.
- `conditional_lead`: a searchable item exists but original wording or metric was not
  verified.
- `unavailable`: the channel failed, is not configured, or no reliable source was found.

User-facing report prose should name natural sources and dates. Do not expose Skill,
API, fallback or routing implementation details in the report body.
