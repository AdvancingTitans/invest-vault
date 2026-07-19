# Search routing

Run `agent-reach doctor --json` first.

- If `exa_search.active_backend` is available, use the Exa command reported by Agent
  Reach for discovery.
- For GitHub repository evidence, use the reported GitHub backend.
- For finance/community discovery, use Xueqiu only when Agent Reach reports an active
  authenticated backend.
- If none is active, do not invent a search command. Continue only with known URLs
  already present in the application evidence pack and record discovery as unavailable.

Search results are leads. A lead becomes evidence only after its source page is read.
