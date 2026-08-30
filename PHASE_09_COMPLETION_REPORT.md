# Phase 09 — Multi-Source Orchestrator — Completion Report

**Repository:** tanmaytalekar13/Discovery-Application
**Plan:** CODEX_EXECUTION_PLAN.md v4.0
**Phase executed:** 09 (Multi-source orchestrator)
**Prior state confirmed:** Phases 00–08 were already implemented (Docker/repo
baseline, ArcadeDB schema, MCP protocol core, A2A protocol core, GitHub
discovery, MCP registry discovery, A2A registry + Well-Known discovery, web
search discovery, safe web extraction). No prior phase's code was modified
except `app/config.py` and `.env.example`, which only had new, additive,
default-off configuration fields appended.

## Definition of Done (Section 39, Phase 09) — verified

- [x] **Sources execute concurrently** — `gather_source_outcomes()`
      (`app/search/concurrency.py`) runs every configured source's coroutine
      via `asyncio.gather`, confirmed by `test_search_concurrency.py::
      test_sources_run_concurrently_not_sequentially` (a fast source finishes
      before a slower one only when both actually run at the same time).
- [x] **One source can fail without breaking search** — each source's
      exception is caught individually and turned into a failed
      `SourceOutcome` instead of propagating. Verified at three levels:
      `test_search_concurrency.py`, `test_mcp_discovery_adapter.py::
      test_one_source_failure_does_not_break_the_others`,
      `test_a2a_discovery_adapter.py::
      test_one_registry_failure_does_not_break_the_other`, and
      `test_discovery_orchestrator.py::
      test_failed_source_is_reported_without_breaking_the_result`.
- [x] **Candidates aggregate correctly** — every source's own candidate type
      (`GitHubCandidate`, `MCPRegistryCandidate`, `A2ARegistryCandidate`,
      `WebSearchCandidate`, `WebExtractionCandidate`, `AgentCardProbeResult`)
      is converted into one common `CandidateReference` (Section 8 schema)
      and flattened into a single result, without inventing any data the
      source did not provide (rule #11). Verified by
      `test_candidate_conversion.py` (one test per source) and
      `test_discovery_orchestrator.py::
      test_runs_mcp_and_a2a_adapters_concurrently_and_aggregates`.

## What was built

| File | Purpose |
|---|---|
| `app/discovery/common/candidate.py` | `CandidateReference` (Section 8) + one pure conversion function per existing source adapter's candidate type. `SourceOutcome` dataclass for per-source isolation reporting. |
| `app/discovery/common/__init__.py` | Package exports. |
| `app/search/concurrency.py` | `gather_source_outcomes()` — the one reusable "run these named coroutines concurrently, isolate each failure" primitive (rule #21), used by both protocol adapters. |
| `app/search/mcp_adapter.py` | `MCPDiscoveryAdapter` — fans a query out to GitHub, MCP Registry, Web Search, Web Extraction and configured direct MCP endpoints concurrently (Section 7 diagram). |
| `app/search/a2a_adapter.py` | `A2ADiscoveryAdapter` — fans a query out to GitHub, one-or-more A2A/agent registries, Web Search, Web Extraction, Well-Known probing and configured direct Agent Card URLs concurrently (Section 7 diagram). |
| `app/search/orchestrator.py` | `DiscoveryOrchestrator` — top-level orchestrator; runs the MCP and A2A adapters concurrently and flattens their `SourceOutcome`s into `SearchOrchestratorResult` (`candidates`, `sources_attempted`, `sources_succeeded`, `sources_failed`, `source_errors`), matching Section 28's search-response `metadata` shape. |
| `app/search/factory.py` | Builds a `DiscoveryOrchestrator` from `Settings`, honoring the `ENABLE_*_DISCOVERY` flags (Section 32/33) — each source is independently on/off, and a source missing its required configuration (e.g. no Brave API key) is treated as not-configured rather than attempted-and-always-failing. |
| `app/search/__init__.py` | Package exports. |
| `app/config.py` | Added `enable_*_discovery` flags, `github_token`, `brave_search_api_key`, comma-separated `a2a_registry_base_urls` / `well_known_agent_hosts` / `configured_mcp_endpoints` / `configured_agent_card_urls`, `discovery_max_results_per_source`. All default to the safe/off values from Section 32's example config. |
| `.env.example` | Documents the new variables. |

## Tests added (all passing)

- `app/tests/test_candidate_conversion.py` — 10 tests, one (or more) per source
  conversion function, including the well-known "not found" → `None` case.
- `app/tests/test_search_concurrency.py` — 3 tests for the isolation primitive.
- `app/tests/test_mcp_discovery_adapter.py` — 4 tests (disabled sources
  skipped, multi-source aggregation, failure isolation, configured
  endpoints).
- `app/tests/test_a2a_discovery_adapter.py` — 4 tests (disabled sources
  skipped, multi-registry aggregation, failure isolation, well-known
  found/not-found filtering).
- `app/tests/test_discovery_orchestrator.py` — 7 tests (construction
  validation, empty-query/invalid-item_type rejection, concurrent
  aggregation across protocol groups, failure reporting, `item_type`
  filtering).
- `app/tests/test_search_factory.py` — 6 tests (default-off, per-source
  enable flags, CSV parsing, combined orchestrator construction).

**Full backend suite:** `161 passed` (123 pre-existing + 38 new), `ruff
check` and `black --check` clean on every file touched in this phase.

## Explicitly out of scope for Phase 09 (left for later phases per Section 44's "implement only that phase")

- Querying ArcadeDB / cold vs. warm search decision (**Phase 17** / Sections
  17–18).
- Deduplication, reliability scoring, and persisting approved items
  (**Phase 10**).
- Ranking (**Phase 11**).
- The REST `/api/search` endpoint and SSE wiring (**Phase 12**) — the
  orchestrator is a plain importable class, not yet mounted on a route.
- The "Optional DNS discovery adapter" branch from Section 7 (listed under
  Section 6's *Additional*, not *Mandatory/core*) — not stubbed, per the
  Codex protocol's "do not create placeholder classes/endpoints merely to
  make a phase appear complete."
- Dynamic web-search → targeted-extraction chaining (Section 2's diagram
  places extraction downstream of search). The current
  `WebExtractionAdapter` wiring only accepts explicitly configured target
  URLs; automatically feeding search results' URLs into extraction is a
  reasonable Phase 10/11 refinement, not required by Phase 09's DoD.

## Suggested next step

Phase 10 — Normalization / deduplication / reliability / persistence: take
`SearchOrchestratorResult.candidates` from this phase, run each through the
existing MCP (`app/discovery/mcp/client.py`) / A2A
(`app/discovery/a2a/client.py`) protocol resolvers where not already
resolved, normalize into `Item`/`Tool`/`Agent`, deduplicate by canonical
identity (never by name), score reliability against the 0.75 threshold, and
persist approved items in ArcadeDB via `app/db/repositories.py`.
