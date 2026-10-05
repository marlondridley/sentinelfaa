# Architecture decision records

Each record says what was decided, what else was considered, what it costs,
and where the code and tests hold it. They are written to be challenged: every
record ends with the limits that remain.

Format: Status, Context, Decision, Alternatives considered, Consequences,
Evidence, Limits. Records are not edited after acceptance; a changed decision
gets a new record that supersedes the old one.

| # | Decision | Status |
|---|---|---|
| [0001](0001-rules-decide-models-explain.md) | Deterministic rules decide; models explain and challenge | Accepted |
| [0002](0002-human-gate-never-auto-executes.md) | A verified person decides every escalation; nothing auto-executes | Accepted |
| [0003](0003-spiffe-x509-mtls-for-workload-identity.md) | Workload identity is SPIFFE X.509-SVID over mutual TLS | Accepted, live since 2026-09-28 |
| [0004](0004-jms-edge-kafka-deferred.md) | Ingest edge is a JMS consumer; Kafka is deferred behind a seam | Accepted |
| [0005](0005-single-model-provider-behind-a-gateway.md) | One model (Claude Haiku) behind a LiteLLM gateway, no fallback | Accepted |
| [0006](0006-verification-from-independent-evidence.md) | Agents never grade themselves; quality comes from independent evidence | Accepted |
| [0007](0007-structured-retrieval-not-vector-search.md) | Agents read by keyed query; no vector search or reranking | Accepted |

Related: [agent-governance.md](../agent-governance.md) (how each boundary is
enforced and tested), [framework-mapping.md](../framework-mapping.md) (NIST AI
RMF and ISO/IEC 42001 coverage).
