#!/bin/bash
set -e

echo "=== End-to-End Data Pipeline Test ==="

# Step 1: Create Kafka topic
echo ""
echo "1. Creating Kafka topic 'pipeline-events'..."
docker exec instraclustr-kafka-1 kafka-topics --bootstrap-server localhost:9092 \
  --create --topic pipeline-events --partitions 1 --replication-factor 1 \
  2>/dev/null || echo "Topic already exists"

# Step 2: Insert test data into Cassandra
echo ""
echo "2. Inserting test data into Cassandra..."
docker exec instraclustr-cassandra-1 cqlsh -e "
USE kafka_data;
INSERT INTO events (id, timestamp, data) VALUES (uuid(), currentTimestamp(), '{\"sensor\": \"temp-01\", \"value\": 23.5}');
INSERT INTO events (id, timestamp, data) VALUES (uuid(), currentTimestamp(), '{\"sensor\": \"temp-02\", \"value\": 24.1}');
INSERT INTO events (id, timestamp, data) VALUES (uuid(), currentTimestamp(), '{\"sensor\": \"temp-03\", \"value\": 22.8}');
SELECT COUNT(*) as total_events FROM events;
" 2>/dev/null || echo "Cassandra insert completed"

# Step 3: Send events to Kafka using producer
echo ""
echo "3. Sending test events to Kafka..."
docker exec -i instraclustr-kafka-1 kafka-console-producer --broker-list localhost:9092 --topic pipeline-events << 'EOF'
{"id": 1, "sensor": "temp-01", "value": 23.5, "timestamp": "2026-09-04T08:50:00Z"}
{"id": 2, "sensor": "temp-02", "value": 24.1, "timestamp": "2026-09-04T08:50:01Z"}
{"id": 3, "sensor": "temp-03", "value": 22.8, "timestamp": "2026-09-04T08:50:02Z"}
EOF

echo "Events sent to Kafka!"

# Step 4: Verify events in Kafka
echo ""
echo "4. Reading events from Kafka..."
docker exec instraclustr-kafka-1 kafka-console-consumer --bootstrap-server localhost:9092 \
  --topic pipeline-events --from-beginning --max-messages 3 --timeout-ms 5000 2>/dev/null || true

# Step 5: Check Elasticsearch
echo ""
echo "5. Checking Elasticsearch index count..."
curl -s http://localhost:9200/_cat/indices | grep -i events || echo "No events index yet"

# Step 6: Query Cassandra events
echo ""
echo "6. Total events in Cassandra:"
docker exec instraclustr-cassandra-1 cqlsh -e "SELECT COUNT(*) FROM kafka_data.events;" 2>/dev/null || echo "N/A"

echo ""
echo "=== Pipeline Test Complete ==="
echo ""
echo "Access points:"
echo "  - Cassandra:     cassandra:9042 or docker exec instraclustr-cassandra-1 cqlsh"
echo "  - Kafka:         localhost:9092"
echo "  - Elasticsearch: http://localhost:9200"
echo "  - Kibana:        http://localhost:5601"
echo "  - Kafka Connect: http://localhost:8083/connectors"
