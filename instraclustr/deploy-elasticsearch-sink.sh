#!/bin/bash

KAFKA_CONNECT_URL="http://localhost:8083"

echo "=== Deploying Elasticsearch Sink Connector ==="

# Read the connector config
CONNECTOR_CONFIG=$(cat <<'EOF'
{
  "name": "elasticsearch-sink-connector",
  "config": {
    "connector.class": "io.confluent.connect.elasticsearch.ElasticsearchSinkConnector",
    "tasks.max": "1",
    "topics": "pipeline-events",
    "connection.url": "http://elasticsearch:9200",
    "type.name": "_doc",
    "key.ignore": "true",
    "schema.ignore": "true",
    "value.converter": "org.apache.kafka.connect.json.JsonConverter",
    "value.converter.schemas.enable": "false"
  }
}
EOF
)

echo "Deploying connector..."
curl -X POST "$KAFKA_CONNECT_URL/connectors" \
  -H "Content-Type: application/json" \
  -d "$CONNECTOR_CONFIG"

echo ""
echo "Connector deployed! Listing all connectors:"
curl -s "$KAFKA_CONNECT_URL/connectors" | head -100
