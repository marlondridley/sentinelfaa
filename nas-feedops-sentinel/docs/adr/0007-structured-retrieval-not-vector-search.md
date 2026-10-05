# 0007. Agents read by keyed query; no vector search or reranking

Status: Accepted (recorded 2026-10-02)

## Context
Agents need context: recent alerts, feed health, flights and evidence for one
investigation. All of it is typed data keyed by airport, flight, alert or
investigation id. There is no corpus of unstructured documents.

## Decision
Agents receive context built by exact queries and bounded projections built
field by field (the Watch Officer's `project_for_model`, the Critic's SQL column
list, the investigation evidence in `src/investigation/evidence.py`). There is no
embedding store, no similarity search, and therefore no top-k to rerank. Ordering
is deterministic (recency, severity), which also keeps context selection
explainable to an assessor and makes prompt contents reproducible.

## Alternatives considered
- **RAG over alerts and notes.** Rejected: exact keys beat similarity for keyed
  data, and similarity can return the wrong airport's rows.
- **Add a reranker for relevance.** Not applicable without a fuzzy retrieval step;
  it would add latency and cost and make the selected context harder to audit.

## Consequences
- No embedding pipeline, vector index or reranker to run, secure or evaluate.
- Retrieval quality is a query-design question, testable with ordinary tests.
- Unstructured sources cannot be searched by meaning today.

## Revisit when
Unstructured sources are added (NOTAMs, advisories, runbooks). Then: hybrid
retrieval (BM25 plus vectors fused with reciprocal rank fusion), a wide top-k
(50 to 100), a cross-encoder rerank to the top 5 to 8, measured with recall@k and
nDCG against a labelled query set, and kept only if the gain beats the added
latency and cost. Log candidates, scores and the final cut for audit.

## Evidence
Context builders named above and their tests (`tests/test_investigation.py`,
`tests/test_critic_agent.py`); the prompt-leak layers in
[agent-governance.md](../agent-governance.md) boundary 1.

## Limits
This is a decision about today's data, not a claim that reranking is unnecessary
in general. It is not built, and nothing here should be presented as if it were.
