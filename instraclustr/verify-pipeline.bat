@echo off
REM Data Pipeline Health Check - Batch Script

echo.
echo ========================================================================
echo   CASSANDRA ^<-^> KAFKA ^<-^> ELASTICSEARCH PIPELINE VERIFICATION
echo ========================================================================
echo.

REM 1. Check Docker Containers
echo 1. DOCKER CONTAINERS
echo ========================================================================

docker ps --filter "label=com.docker.compose.project=instraclustr" --format "table {{.Names}}\t{{.Status}}"

echo.
echo 2. CASSANDRA
echo ========================================================================

docker exec instraclustr-cassandra-1 cqlsh localhost 9042 -e "DESC KEYSPACES;" 2>nul | find "kafka_data" >nul
if %errorlevel% equ 0 (
    echo OK - Keyspace 'kafka_data' exists
) else (
    echo FAIL - Keyspace 'kafka_data' not found
)

echo.
echo 3. KAFKA
echo ========================================================================

docker exec instraclustr-kafka-1 kafka-topics --bootstrap-server localhost:9092 --list 2>nul | find "pipeline-events" >nul
if %errorlevel% equ 0 (
    echo OK - Topic 'pipeline-events' exists
) else (
    echo FAIL - Topic 'pipeline-events' not found
)

echo.
echo 4. KAFKA CONNECT
echo ========================================================================

curl -s http://localhost:8083/connectors 2>nul | find "elasticsearch" >nul
if %errorlevel% equ 0 (
    echo OK - Elasticsearch connector deployed
) else (
    echo FAIL - Elasticsearch connector not found
)

echo.
echo 5. ELASTICSEARCH
echo ========================================================================

curl -s http://localhost:9200/_cat/indices 2>nul | find "pipeline-events" >nul
if %errorlevel% equ 0 (
    echo OK - Index 'pipeline-events' exists
    curl -s http://localhost:9200/pipeline-events/_count 2>nul | find "count" >nul
    if %errorlevel% equ 0 (
        echo OK - Documents indexed successfully
    )
) else (
    echo FAIL - Index 'pipeline-events' not found
)

echo.
echo 6. KIBANA
echo ========================================================================

curl -s http://localhost:5601/api/status 2>nul | find "version" >nul
if %errorlevel% equ 0 (
    echo OK - Kibana responsive
) else (
    echo FAIL - Kibana not responding
)

echo.
echo ========================================================================
echo VERIFICATION COMPLETE
echo ========================================================================
echo.
echo Access Points:
echo   Cassandra:      docker exec instraclustr-cassandra-1 cqlsh
echo   Kafka:          localhost:9092
echo   Elasticsearch:  http://localhost:9200
echo   Kibana:         http://localhost:5601
echo   Kafka Connect:  http://localhost:8083
echo.
echo Documentation:
echo   PIPELINE_GUIDE.md - Complete operational reference
echo   DEPLOYMENT_SUMMARY.md - Deployment details
echo.
