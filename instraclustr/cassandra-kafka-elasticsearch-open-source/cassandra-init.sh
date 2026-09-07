#!/bin/bash
set -e

cat >/import.cql <<'EOF'
DROP KEYSPACE IF EXISTS test;
CREATE KEYSPACE test WITH replication = {'class':'SimpleStrategy', 'replication_factor' : 1} AND durable_writes = true;
CREATE TABLE test.clock ( 
	dayOfTheWeek text,
	currentDateTime text,
	currentFileTime timestamp,
	PRIMARY KEY (currentFileTime)
);
EOF
# 2. Run schema execution loop in background
(
  until cqlsh 127.0.0.1 9042 -f /import.cql >/dev/null 2>&1; do
    echo "cqlsh: Cassandra is unavailable to initialize - will retry later"
    sleep 2
  done
  echo "cqlsh: Initialization script completed successfully."
) &

# 3. Hand control back to official docker entrypoint
exec docker-entrypoint.sh cassandra -f