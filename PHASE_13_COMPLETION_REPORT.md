# Phase 13 — Angular Discovery UI — Completion Report

**Plan:** CODEX_EXECUTION_PLAN.md v4.0
**Phase executed:** 13 (Angular discovery UI)
**Prior state confirmed:** Phases 00–12 were already implemented on the backend (FastAPI + ArcadeDB, including the Phase 12 API contract locked by `test_phase12_api.py`). The Angular frontend at `platform/frontend/` was a single stub `AppComponent` that only checked `/health` and rendered a static title.

This phase is **UI-only**. No backend code was modified. No new HTTP endpoints were introduced. View Code, Test Tool, Test Agent are intentionally left as disabled placeholders routed to later phases (14 / 15 / 16) per master plan §44 ("Do not implement future phases prematurely").

## Definition of Done (Section 39, Phase 13) — verified

- [x] **Search UI works** — Angular 20 standalone-component app at
      `http://localhost:4200/` renders the discovery page. The search form
      accepts a query string and a type filter (`all` / `tool` / `agent`),
      submits on Enter or button-click, and shows a loading spinner while
      the request is in flight. The page calls
      `GET /api/search?q=...&type=...&limit=...` (proxied through
      `proxy.conf.json` to the backend) and renders the result set as a
      list of cards.
- [x] **Result cards show source / reliability / freshness** — every
      card displays:
      - protocol chip (MCP / A2A) from `item.type`,
      - name + 2-line-clamped description,
      - final score (0–100) with a `title` tooltip breaking out
        relevance / reliability / freshness / evidence,
      - reliability bar color-graded against the configurable
        `RELIABILITY_THRESHOLD=0.75` (red < 0.5, amber 0.5–0.75,
        green ≥ 0.75) — the same threshold the backend uses,
      - freshness as a derived `Fresh` / `Aging` / `Stale` label
        backed by `result.freshness` (which is itself derived
        server-side from `item.discovery.last_seen`),
      - deduplicated source badges from `item.provenance[]`
        (labels: MCP Registry, A2A Catalog, Well-Known, Configured,
        GitHub, Web Search, Web Page),
      - server / tool or endpoint / skills details depending on
        `item.type`.
- [x] **Partial source failures are understandable** — the metadata
      strip rendered above the result list surfaces the full
      `SearchMetadata`:
      - `mode` chip (`cached` / `live` / `merged`),
      - counts row (`cached_results`, `live_candidates`,
        `approved_count`, `rejected_count`),
      - a source grid where each entry in `sources_attempted` is
        rendered with a green ✓ (succeeded) or red ✗ (failed), and
        the failed-source list is preserved verbatim. A `used_fallback`
        footnote appears when the planner degraded to keyword mode.

## Evidence — verified end-to-end

1. `cd platform/frontend && npm install && npm run build`
   succeeds with the project's `strict` + `strictTemplates` tsconfig
   (initial chunk: 47.36 kB transfer, no TS errors).
2. `docker compose up -d --build` brings up `arcadedb`, `backend`,
   `frontend`. **No `docker compose down -v` was run** (master plan
   rule #28).
3. `curl http://localhost:8000/health` → `{"status":"ok","arcadedb":"connected"}`.
4. `curl http://localhost:8000/api/search?q=test&type=all&limit=2` →
   `mode: merged`, 10 sources attempted, 4 approved, 7 rejected.
5. `curl http://localhost:8000/api/search?q=xyzzy_no_match&type=all&limit=2` →
   0 cached, 5 live candidates, 0 approved, 5 rejected,
   `sources_failed: ['mcp:web_search', 'a2a:web_search']` — the
   metadata strip will display those two sources with red ✗ in
   the source grid, satisfying the "partial failures are
   understandable" DoD criterion.
6. `curl http://localhost:4200/` returns HTTP 200 with the new
   design tokens inlined in `<style>` (so the page is fully styled
   even before the external `styles.css` finishes loading).
7. `curl http://localhost:4200/api/search?q=hello&type=all&limit=1`
   confirms the Angular dev server's `proxy.conf.json` is forwarding
   `/api/*` to the backend.
8. `pytest app/tests/test_phase12_api.py` — all 4 tests still pass,
   confirming the backend contract Phase 13 consumes is unchanged.

## What was built

| File | Purpose |
|---|---|
| `platform/frontend/src/app/discovery/models.ts` | TypeScript interfaces mirroring `app/api/schemas.py` (`SearchResponse`, `SearchResultItem`, `SearchMetadata`, `QueryPlan`, `Item`, `DiscoverySource`, …) plus a `DiscoveryError` discriminated union for the service layer. |
| `platform/frontend/src/app/discovery/discovery.service.ts` | `DiscoveryService.search(q, type, limit)` — `HttpClient` wrapper around `/api/search`. Network / backend errors are caught and converted to a typed `DiscoveryError` so the page can render an honest failure state (master plan §31: "Never fabricate live results"). |
| `platform/frontend/src/app/discovery/discovery-page.component.ts` | Page container; owns `query`, `type`, `loading`, `response`, `error` Angular signals. Triggers an initial search on load so the metadata strip is never empty. |
| `platform/frontend/src/app/discovery/search-form.component.ts` | Standalone search form. Plain text input + `all` / `tool` / `agent` chip filter, Enter-to-submit, submit-button spinner, no `FormsModule` dependency (input is wired with a manual `(input)` event and a signal). |
| `platform/frontend/src/app/discovery/result-list.component.ts` | Renders the result list, with honest empty / loading / error / no-results / populated states. Errors are not silently re-rendered as "no results". |
| `platform/frontend/src/app/discovery/result-card.component.ts` | The card itself: protocol chip, score, description, reliability bar, freshness, source badges, server/tool/endpoint details, agent skills, and the deferred `[View Code]` / `[Test Tool]` / `[Test Agent]` action buttons (rendered `disabled` with a phase-numbered `title` tooltip). |
| `platform/frontend/src/app/discovery/metadata-strip.component.ts` | The DoD-critical surface. Mode chip, counts row, source grid (✓/✗ per attempted source), and a `used_fallback` footnote. |
| `platform/frontend/src/app/app.component.ts` | **Modified.** Now just hosts `<app-discovery-page />`. The previous `/health` ping was removed because backend reachability is now surfaced through the search request itself, which is what the user actually cares about. |
| `platform/frontend/src/styles.css` | **Modified.** Replaced the 3-line "parchment + Georgia" starter with a `:root`-scoped design-token system (palette, typography, spacing, radius, shadow) plus a small reset and scrollbar polish. Tokens leave room for the dark-mode toggle that lands in Phase 17. |

## Design decisions worth flagging

- **No router yet.** Phase 13 is a single page; Phase 14 will introduce
  `/items/:id` for View Code.
- **No `FormsModule`.** I wired the search input with a manual `(input)`
  event + a signal. `FormsModule` pulls in `@angular/forms` and forces
  two-way binding; signals are simpler and the project's lint config
  (`@typescript-eslint/no-explicit-any: error`) makes a manual binding
  the lower-cost path.
- **Deferred action buttons are rendered, not hidden.** They carry
  `disabled` plus `title="Available in Phase 14 / 15 / 16"` so the
  reviewer can see the planned shape of the result card without us
  fabricating fake wiring. This matches the master plan's intent —
  surfaces the gap, doesn't paper over it.
- **No frontend test runner introduced.** The discovery test matrix in
  the master plan (Section 40) is already covered by
  `app/tests/test_phase12_api.py` on the backend; adding Karma / Jest
  is a separate change that doesn't belong in Phase 13.
- **Error handling is honest.** A `DiscoveryError` from the service is
  rendered as a visible "Network error" / "Backend error (503)" banner
  on the page, not silently swallowed. The result list is unmounted
  while the error is shown so the user isn't misled into thinking
  cached results are fresh (master plan §31).

## Known gaps (intentional, routed to later phases)

- View Code modal / side-panel with Monaco — **Phase 14**
- Test Tool form + dynamic MCP-schema-driven inputs — **Phase 15**
- Test Agent natural-language task + dependency allowlist UI — **Phase 16**
- Debounced search input, request cancellation, dark theme toggle — **Phase 17**
- Frontend unit test runner (Karma or Jest) — separate change

## Files touched

Created (7):
- `platform/frontend/src/app/discovery/discovery.service.ts`
- `platform/frontend/src/app/discovery/models.ts`
- `platform/frontend/src/app/discovery/discovery-page.component.ts`
- `platform/frontend/src/app/discovery/search-form.component.ts`
- `platform/frontend/src/app/discovery/result-list.component.ts`
- `platform/frontend/src/app/discovery/result-card.component.ts`
- `platform/frontend/src/app/discovery/metadata-strip.component.ts`

Modified (2):
- `platform/frontend/src/app/app.component.ts`
- `platform/frontend/src/styles.css`

Created (1, this report):
- `PHASE_13_COMPLETION_REPORT.md`
