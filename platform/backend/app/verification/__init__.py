"""MCP Server Discovery & Verification subsystem (spec v2).

Layout:
- models.py          statuses, server record, provider credential, decisions
- repository.py      ArcadeDB repositories (McpServer scored registry, etc.)
- prefilter.py       static prefilter incl. Smithery exclusion (Sections 4, 7.4)
- ingestion.py       candidate normalization + dedupe (Section 3)
- verifiers.py       local/remote verification pipeline (Section 5)
- timeout_policy.py  per-stage/per-tool budgets (Section 6)
- scoring.py         weighted scoring + status buckets (Section 8)
- semantic_judge.py  LLM-judge semantic validation (Section 5.3)
- queue.py           background workers, TTL recheck, cold-miss cascade (5, 9.1, 11)
- api.py             read-only search API + review queue + OAuth consent (9, 10)

Golden rule: search only ever SELECTs from the registry; every write
happens in the background pipeline.
"""
