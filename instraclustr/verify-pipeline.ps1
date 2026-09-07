#!/usr/bin/env pwsh
# Data Pipeline Verification & Health Check

Write-Host "`n" 
Write-Host "╔════════════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║   CASSANDRA ↔ KAFKA ↔ ELASTICSEARCH PIPELINE VERIFICATION     ║" -ForegroundColor Green
Write-Host "║                    Health Check Script                         ║" -ForegroundColor Green
Write-Host "╚════════════════════════════════════════════════════════════════╝" -ForegroundColor Green

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host "Timestamp: $timestamp`n"

# 1. DOCKER CONTAINERS
Write-Host "1. DOCKER CONTAINERS" -ForegroundColor Cyan
Write-Host ("=" * 60)

$containers = @(
    "instraclustr-cassandra-1",
    "instraclustr-kafka-1",
    "instraclustr-zookeeper-1",
    "instraclustr-elasticsearch-1",
    "instraclustr-kibana-1",
    "instraclustr-kafka-connect-1"
)

foreach ($container in $containers) {
    $status = docker ps --filter "name=$container" --format "{{.Status}}" 2>$null
    if ($status) {
        Write-Host "  OK $container : $status" -ForegroundColor Green
    } else {
        Write-Host "  FAIL $container : NOT RUNNING" -ForegroundColor Red
    }
}

# 2. CASSANDRA
Write-Host "`n2. CASSANDRA" -ForegroundColor Cyan
Write-Host ("=" * 60)

try {
    $cassandra_check = docker exec instraclustr-cassandra-1 cqlsh localhost 9042 -e "DESC KEYSPACES;" 2>&1
    if ($cassandra_check -match "kafka_data") {
        Write-Host "  OK Cassandra: HEALTHY" -ForegroundColor Green
        Write-Host "  OK Keyspace 'kafka_data': EXISTS" -ForegroundColor Green
    } else {
        Write-Host "  WARN Cassandra: MISSING KEYSPACE" -ForegroundColor Yellow
    }
}
catch {
    Write-Host "  FAIL Cassandra: CONNECTION ERROR" -ForegroundColor Red
}

# 3. KAFKA
Write-Host "`n3. KAFKA" -ForegroundColor Cyan
Write-Host ("=" * 60)

try {
    $kafka_topics = docker exec instraclustr-kafka-1 kafka-topics --bootstrap-server localhost:9092 --list 2>&1
    if ($kafka_topics -match "pipeline-events") {
        Write-Host "  OK Kafka: HEALTHY" -ForegroundColor Green
        Write-Host "  OK Topic 'pipeline-events': EXISTS" -ForegroundColor Green
    } else {
        Write-Host "  WARN Kafka: TOPIC NOT FOUND" -ForegroundColor Yellow
    }
}
catch {
    Write-Host "  FAIL Kafka: CONNECTION ERROR" -ForegroundColor Red
}

# 4. KAFKA CONNECT
Write-Host "`n4. KAFKA CONNECT" -ForegroundColor Cyan
Write-Host ("=" * 60)

try {
    $connectors = curl.exe -s http://localhost:8083/connectors 2>&1 | ConvertFrom-Json
    Write-Host "  OK Kafka Connect: RESPONSIVE" -ForegroundColor Green
    Write-Host "  INFO Connectors deployed: $($connectors.Count)" -ForegroundColor White
    
    foreach ($connector in $connectors) {
        $conn_status = curl.exe -s "http://localhost:8083/connectors/$connector/status" 2>&1 | ConvertFrom-Json
        $state = $conn_status.connector.state
        if ($state -eq "RUNNING") {
            Write-Host "     OK $connector : $state" -ForegroundColor Green
        } else {
            Write-Host "     WARN $connector : $state" -ForegroundColor Yellow
        }
    }
}
catch {
    Write-Host "  FAIL Kafka Connect: CONNECTION ERROR" -ForegroundColor Red
}

# 5. ELASTICSEARCH
Write-Host "`n5. ELASTICSEARCH" -ForegroundColor Cyan
Write-Host ("=" * 60)

try {
    $es_health = curl.exe -s http://localhost:9200/_cluster/health 2>&1 | ConvertFrom-Json
    $es_status = $es_health.status
    Write-Host "  OK Elasticsearch: RESPONSIVE (status: $es_status)" -ForegroundColor Green
    
    $indices = curl.exe -s http://localhost:9200/_cat/indices 2>&1
    if ($indices -match "pipeline-events") {
        Write-Host "  OK Index 'pipeline-events': EXISTS" -ForegroundColor Green
        $doc_count = (curl.exe -s http://localhost:9200/pipeline-events/_count 2>&1 | ConvertFrom-Json).count
        Write-Host "  INFO Documents indexed: $doc_count" -ForegroundColor White
    } else {
        Write-Host "  WARN Index 'pipeline-events': NOT FOUND" -ForegroundColor Yellow
    }
}
catch {
    Write-Host "  FAIL Elasticsearch: CONNECTION ERROR" -ForegroundColor Red
}

# 6. KIBANA
Write-Host "`n6. KIBANA" -ForegroundColor Cyan
Write-Host ("=" * 60)

try {
    $kibana_status = curl.exe -s http://localhost:5601/api/status 2>&1 | ConvertFrom-Json
    Write-Host "  OK Kibana: RESPONSIVE" -ForegroundColor Green
    Write-Host "  INFO Available at http://localhost:5601" -ForegroundColor White
}
catch {
    Write-Host "  FAIL Kibana: CONNECTION ERROR" -ForegroundColor Red
}

# 7. SUMMARY
Write-Host "`n" 
Write-Host ("=" * 60) -ForegroundColor Green
Write-Host "VERIFICATION COMPLETE" -ForegroundColor Green
Write-Host ("=" * 60) -ForegroundColor Green

Write-Host "`nAccess Points:" -ForegroundColor Yellow
Write-Host "  Cassandra:      localhost:9042 or 'docker exec instraclustr-cassandra-1 cqlsh'" 
Write-Host "  Kafka:          localhost:9092"
Write-Host "  Elasticsearch:  http://localhost:9200"
Write-Host "  Kibana:         http://localhost:5601"
Write-Host "  Kafka Connect:  http://localhost:8083"

Write-Host "`nDocumentation:" -ForegroundColor Yellow
Write-Host "  See PIPELINE_GUIDE.md for complete reference"
Write-Host "  See DEPLOYMENT_SUMMARY.md for deployment details"

Write-Host "`n"
