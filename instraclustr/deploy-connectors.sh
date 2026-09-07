#!/bin/bash
set -e

KAFKA_CONNECT_URL="http://localhost:8083"
CASSANDRA_CONNECTOR_FILE="cassandra-source-connector.json"
ELASTICSEARCH_CONNECTOR_FILE="elasticsearch-sink-connector.json"

echo "=== Deploying Kafka Connectors ==="

# Wait for Kafka Connect to be ready
echo "Waiting for Kafka Connect to be ready..."
max_retries=30
retries=0
while ! curl -s "$KAFKA_CONNECT_URL/connectors" > /dev/null 2>&1; do
  retries=$((retries + 1))
  if [ $retries -ge $max_retries ]; then
    echo "Kafka Connect did not become ready after $max_retries attempts"
    exit 1
  fi
  echo "Attempt $retries/$max_retries..."
  sleep 2
done

echo "Kafka Connect is ready!"

# Deploy Cassandra Source Connector
echo ""
echo "Deploying Cassandra Source Connector..."
curl -X POST "$KAFKA_CONNECT_URL/connectors" \
  -H "Content-Type: application/json" \
  -d @"$CASSANDRA_CONNECTOR_FILE"
echo ""
echo "Cassandra Source Connector deployed!"

# Wait a bit for the connector to initialize
sleep 5

# Deploy Elasticsearch Sink Connector
echo ""
echo "Deploying Elasticsearch Sink Connector..."
curl -X POST "$KAFKA_CONNECT_URL/connectors" \
  -H "Content-Type: application/json" \
  -d @"$ELASTICSEARCH_CONNECTOR_FILE"
echo ""
echo "Elasticsearch Sink Connector deployed!"

# List deployed connectors
echo ""
echo "=== Deployed Connectors ==="
curl -s "$KAFKA_CONNECT_URL/connectors" | jq '.'

echo ""
echo "=== Connector Deployment Complete ==="
