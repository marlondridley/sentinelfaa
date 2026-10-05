# 0004. Ingest edge is a JMS consumer; Kafka is deferred behind a seam

Status: Accepted (decided 2026-09-26)

## Context
The FAA's SWIM Cloud Distribution Service guideline authorizes JMS as the only
consumer API. The supplied Jumpstart Kit is a JVM that already speaks it. The
project is a beta, not a demo, so ingest must not lose messages.

## Decision
The edge stays the Jumpstart Kit JVM. It writes one `swim-raw/1` record per
message (`message_id`, `headers`, `body`, `received_at`), and Python reads
through a small interface, `RawMessageSource` (`poll`, `commit`, `quarantine`)
in `src/raw_message.py`. Today the only source is a folder of record files. There
is no direct Python AMQP consumer. `commit()` runs only after the events are
durably stored, so delivery is at-least-once, and idempotent event ids make a
redelivery harmless. This closes the kit's acknowledge-before-write gap.

## Alternatives considered
- **Python AMQP consumer.** Rejected: outside what the SCDS guideline authorizes.
- **Kafka between the JVM and Python now.** Deferred: it adds a cluster to run,
  secure and monitor with no second consumer to justify it.
- **Process in the JVM.** Rejected: the parsing and rules live in Python with the
  rest of the tests.

## Consequences
- A broker-backed source (Kafka, for example) is a second implementation of three
  methods plus a producer in the JVM output, not a rewrite.
- Folder hand-off means two processes share a directory; the single-writer design
  between the watcher and the ingest workflow is still an open decision.
- Multi-airport scope is configuration (`SUPPORTED_AIRPORTS`, `src/airports.py`),
  and is not a reason to add a broker.

## Revisit when
A second consumer of the raw stream exists, or a second feed (TFMS, TBFM) must be
ingested alongside this one.

## Evidence
`tests/test_swim_kit_ingest.py` (the ten real FAA captures, ten for ten);
`src/raw_message.py` for the interface and its delivery contract.

## Limits
This is a single-feed beta. Throughput and backpressure at multi-feed scale have
not been measured.
