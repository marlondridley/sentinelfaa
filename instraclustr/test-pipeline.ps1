# PowerShell Pipeline Integration Test
$ErrorActionPreference = "Stop"

Write-Host "=== Testing Cassandra/Kafka/Elasticsearch Data Pipeline ===" -ForegroundColor Cyan

# 1. Insert test data into Cassandra
Write-Host "`n1. Inserting test data into Cassandra..." -ForegroundColor Yellow
$cql = "USE kafka_data; INSERT INTO events (id, timestamp, data) VALUES (uuid(), currentTimestamp(), 'PowerShell verification event'); SELECT id, timestamp, data FROM events LIMIT 3;"
docker exec instraclustr-cassandra-1 cqlsh -e $cql
Write-Host "Cassandra insert verified." -ForegroundColor Green

# 2. Produce test messages to Kafka topic 'pipeline-events'
Write-Host "`n2. Producing test event to Kafka 'pipeline-events'..." -ForegroundColor Yellow
$testEvent = @{
    id = "ps-test-$(Get-Random -Minimum 1000 -Maximum 9999)"
    sensor = "sensor-ps-01"
    value = 28.7
    timestamp = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json -Compress

$testEvent | docker exec -i instraclustr-kafka-1 kafka-console-producer --broker-list localhost:9092 --topic pipeline-events
Write-Host "Produced event to Kafka: $testEvent" -ForegroundColor Green

# 3. Check Kafka Connect connector status
Write-Host "`n3. Checking Kafka Connect connector status..." -ForegroundColor Yellow
try {
    $connectorName = if ((Invoke-RestMethod -Uri "http://localhost:8083/connectors") -contains "elasticsearch-confluent-sink") { "elasticsearch-confluent-sink" } else { "elasticsearch-sink-connector" }
    $status = Invoke-RestMethod -Uri "http://localhost:8083/connectors/$connectorName/status" -Method Get
    Write-Host "Connector ($connectorName) State: $($status.connector.state), Task State: $($status.tasks[0].state)" -ForegroundColor Green
} catch {
    Write-Host "Warning: Could not fetch connector status. Ensure connector is deployed." -ForegroundColor Yellow
}

# 4. Verify Elasticsearch received the indexed documents
Write-Host "`n4. Verifying Elasticsearch indexed documents..." -ForegroundColor Yellow
$maxRetries = 15
$retries = 0
$docCount = 0

while ($retries -lt $maxRetries) {
    try {
        $countResponse = Invoke-RestMethod -Uri "http://localhost:9200/pipeline-events/_count" -Method Get
        $docCount = $countResponse.count
        if ($docCount -gt 0) {
            Write-Host "Success: Found $docCount documents in Elasticsearch 'pipeline-events' index!" -ForegroundColor Green
            break
        }
    } catch {
        # Index might not be created yet
    }
    Write-Host "Waiting for documents in Elasticsearch (attempt $($retries + 1)/$maxRetries)..."
    Start-Sleep -Seconds 2
    $retries++
}

if ($docCount -eq 0) {
    Write-Host "ERROR: No documents indexed in Elasticsearch after $maxRetries attempts." -ForegroundColor Red
    exit 1
}

# 5. Display sample document
Write-Host "`n5. Sample Elasticsearch Hits:" -ForegroundColor Yellow
$search = Invoke-RestMethod -Uri "http://localhost:9200/pipeline-events/_search?size=2" -Method Get
$search.hits.hits | ForEach-Object {
    Write-Host "Doc ID: $($_.id) - Source: $($_.source | ConvertTo-Json -Compress)"
}

Write-Host "`n=== Pipeline Integration Test Passed Successfully! ===" -ForegroundColor Cyan
