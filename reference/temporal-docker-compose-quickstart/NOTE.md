# What this folder is

This is the unmodified **Temporal** project's official `docker-compose` quickstart
repo (https://github.com/temporalio/docker-compose), not anything from Instaclustr.
It was originally copied in under `instraclustr/docker-compose/`, which was
confusing -- it has nothing to do with the Instaclustr-flavored infra stack this
project actually runs.

## Is it used by the launcher?

No. `launch_nas_sentinel.ps1` and `instraclustr/docker-compose.yml` do not reference
anything in this folder. It's kept purely as reference material for when this
project's Temporal integration moves past `temporal_worker.py`'s current
deferred/not-yet-wired-up state.

## What's in it

Multiple pre-built compose variants for running a standalone Temporal server
locally against different backing stores (Cassandra, MySQL, Postgres/OpenSearch,
TLS-enabled variants), plus an observability stack (Grafana dashboards,
Prometheus, Loki, OTel config) under `deployment/`.

If you end up not needing local Temporal at all, this whole folder can be
deleted -- it can always be re-cloned from the upstream repo above.
