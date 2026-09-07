#!/bin/bash
set -e

echo "=== Testing Cassandra/Kafka/Elasticsearch Data Pipeline ==="

# 1. Insert test data into Cassandra
echo ""
echo "1. Inserting test data into Cassandra..."
docker exec instraclustr-cassandra-1 cqlsh -e "
USE kafka_data;
INSERT INTO events (id, timestamp, data) VALUES (uuid(), currentTimestamp(), 'Pipeline verification event 1');
INSERT INTO events (id, timestamp, data) VALUES (uuid(), currentTimestamp(), 'Pipeline verification event 2');
SELECT id, timestamp, data FROM events LIMIT 5;
"

# 2. Ensure Kafka topic 'pipeline-events' exists
echo ""
echo "2. Ensuring Kafka topic 'pipeline-events' exists..."
docker exec instraclustr-kafka-1 kafka-topics --bootstrap-server localhost:9092 \
  --create --topic pipeline-events --partitions 1 --replication-factor 1 2>/dev/null || echo "Topic 'pipeline-events' already exists"

# 3. Produce test messages to Kafka topic 'pipeline-events'
echo ""
echo "3. Producing test events to Kafka 'pipeline-events'..."
docker exec -i instraclustr-kafka-1 kafka-console-producer --broker-list localhost:9092 --topic pipeline-events << 'EOF'
{"id":"ci-test-1","sensor":"sensor-temp-01","value":25.4,"timestamp":"2026-09-04T09:00:00Z"}
{"id":"ci-test-2","sensor":"sensor-temp-02","value":26.1,"timestamp":"2026-09-04T09:00:01Z"}
EOF
echo "Produced 2 test events to Kafka."

# 4. Check Kafka Connect connector status
echo ""
echo "4. Checking Kafka Connect connector status..."
curl -s http://localhost:8083/connectors | jq .
CONNECTOR_NAME=$(curl -s http://localhost:8083/connectors | jq -r '.[] | select(. | contains("elastic"))' | head -n 1)
if [ -n "$CONNECTOR_NAME" ]; then
  curl -s "http://localhost:8083/connectors/$CONNECTOR_NAME/status" | jq .
fi

# 5. Verify Elasticsearch received the indexed documents
echo ""
echo "5. Verifying Elasticsearch indexed documents..."
max_retries=15
retries=0
count=0

while [ $retries -lt $max_retries ]; do
  count=$(curl -s http://localhost:9200/pipeline-events/_count 2>/dev/null | jq -r '.count // 0' 2>/dev/null || echo 0)
  if [ "$count" -gt 0 ]; then
    echo "Found $count documents in Elasticsearch 'pipeline-events' index!"
    break
  fi
  echo "Waiting for documents to appear in Elasticsearch (attempt $((retries+1))/$max_retries)..."
  sleep 2
  retries=$((retries+1))
done

if [ "$count" -eq 0 ]; then
  echo "ERROR: No documents indexed in Elasticsearch after $max_retries attempts."
  exit 1
fi

echo ""
echo "Sample indexed documents from Elasticsearch:"
curl -s "http://localhost:9200/pipeline-events/_search?pretty&size=2" | jq '.hits.hits'

echo ""
echo "=== Pipeline Integration Test Passed Successfully! ==="
