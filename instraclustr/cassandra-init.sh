#!/bin/bash
set -e

echo "Waiting for Cassandra..."

until cqlsh cassandra 9042 -e "DESCRIBE CLUSTER;" >/dev/null 2>&1; do
    echo "Cassandra is not ready - retrying in 5 seconds..."
    sleep 5
done

echo "Cassandra is ready."

cqlsh cassandra 9042 << 'EOF'
CREATE KEYSPACE IF NOT EXISTS kafka_data 
  WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};

CREATE TABLE IF NOT EXISTS kafka_data.events (
  id UUID PRIMARY KEY,
  timestamp TIMESTAMP,
  data TEXT
);

DESCRIBE TABLES IN kafka_data;
EOF

echo "Cassandra initialization complete."
