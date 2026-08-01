# mem0-mcp

An MCP server wrapping a self-hosted [mem0](https://github.com/mem0ai/mem0) REST API, adding two things mem0 doesn't do on its own:

- **Exact-content dedupe** — identical adds within a time window are skipped before they reach mem0, saving an LLM extraction call per duplicate.
- **Mention-aware retrieval reranking** — memories that get reconfirmed over time rank higher than a pure similarity search would put them, without needing any extra calls to mem0 itself.

## why reranking

mem0's default search ranks purely on embedding similarity. That's fine at low memory counts, but once you're into the hundreds, a fact that's been independently reconfirmed many times (e.g. "user prefers X") and a fact mentioned once in passing score identically if they're similarly worded. This adds a second signal: how often has this memory actually been reinforced, and how recently.

**How it works:**

1. `add_memory` first runs a similarity search for the new content against existing memories. A candidate scoring above `REINFORCEMENT_PREFILTER_THRESHOLD` isn't bumped directly — similarity score alone isn't precise enough (tested live: a genuine paraphrase scored 0.833 against its true target, while a separately-worded, topically-adjacent query scored 0.836 against a *different, unrelated* memory — both land in the same narrow band, so no single threshold cleanly separates a real match from a false one, since short facts sharing a subject/domain cluster tightly in embedding space regardless of whether they're really "the same" fact). Instead the judge LLM (`JUDGE_LLM_BASE_URL`) is asked directly whether the new content restates the candidate as the same fact; only a yes bumps its mention count (debounced to at most once per memory per window, so a single conversation re-mentioning the same fact several times doesn't inflate the count). Leaving `JUDGE_LLM_BASE_URL` unset disables this entirely rather than falling back to an unreliable threshold guess. Only after this does `add_memory` call mem0's own `/memories` endpoint to actually store the content — this detects reinforcement itself rather than relying on mem0's response to signal it, since in practice mem0's `/memories` response doesn't reliably surface it: near-duplicate content it classifies as "no update needed" is silently omitted from the response entirely (no event, no id), and a genuine `UPDATE` event only fires for a narrow same-memory-text-update judgment call that's rare in normal use.
2. `search_memories` over-fetches candidates from mem0's similarity search, then rescales each by:

   ```
   score = similarity × (1 + α · log(1 + mentions)) × recency
   recency = 0.5 + 0.5 · exp(-days_since_update / halflife)
   ```

   Recency has a 0.5 floor so old memories never fully vanish, just get deprioritized. All of this happens locally against a small SQLite table — no extra round-trips to mem0.

Mention counts are stored locally (SQLite, not in mem0's own metadata), which means they're only visible to whatever's calling through this MCP server — other clients hitting mem0 directly won't see or benefit from them. That's a deliberate tradeoff: writing mention counts back into mem0's own metadata would require a read-modify-write on every reinforcement (mem0's update endpoint needs the full text), which is a lot of extra complexity and API calls for a signal that's only used by this reranking step anyway.

## tools exposed

| tool | description |
|---|---|
| `add_memory(content)` | Store a memory (deduped) |
| `search_memories(query)` | Semantic search, reranked |
| `get_all_memories()` | List everything stored |
| `update_memory(memory_id, content)` | Replace a memory's text |
| `delete_memory(memory_id)` | Delete a memory |

## compatibility

Tested against a self-hosted mem0 server built from `mem0ai` 2.0.6. The reinforcement-detection approach (see above) deliberately doesn't depend on the `event` field mem0's `/memories` response returns, so it isn't tied to a specific mem0 version — it only needs the standard `/memories` and `/search` endpoints, which have been stable across mem0's 2.x releases including the newer ADD-only consolidation model (2.0.15+, where `/memories` always returns `event: "ADD"` and never `UPDATE`/`NONE`).

## setup

Requires a running mem0 REST API server (see [mem0ai/mem0](https://github.com/mem0ai/mem0) or your own deployment) reachable from this container.

```bash
cp .env.example .env
# fill in MEM0_URL and MEM0_USER_ID at minimum
docker compose up -d
```

Transport is [streamable-http](https://modelcontextprotocol.io/) by default, listening on `:8001`. Set `MCP_TRANSPORT=stdio` for stdio instead.

All tuning knobs (dedupe window, mention debounce, reranking weights) are environment-overridable — see `.env.example` — no rebuild required to change them.

## license

MIT
