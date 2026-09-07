$connectUrl = "http://localhost:8083"

$config = @{
    name = "elasticsearch-sink-connector"
    config = @{
        "connector.class" = "io.confluent.connect.elasticsearch.ElasticsearchSinkConnector"
        "tasks.max" = "1"
        "topics" = "pipeline-events"
        "connection.url" = "http://elasticsearch:9200"
        "type.name" = "_doc"
        "key.ignore" = "true"
        "schema.ignore" = "true"
        "value.converter" = "org.apache.kafka.connect.json.JsonConverter"
        "value.converter.schemas.enable" = "false"
    }
}

$json = $config | ConvertTo-Json -Depth 10
Write-Host "Deploying connector..."

try {
    $response = Invoke-WebRequest -Uri "$connectUrl/connectors" `
        -Method Post `
        -ContentType "application/json" `
        -Body $json `
        -ErrorAction Stop

    Write-Host "Connector deployed successfully!"
    Write-Host "Response: $($response.Content)"
} catch {
    Write-Host "Deployment result: $($_.Exception.Response.StatusCode)"
    Write-Host "Response: $($_.Exception.Response | Get-Content -Raw)"
}

Write-Host ""
Write-Host "Listing all connectors..."
try {
    $result = Invoke-WebRequest -Uri "$connectUrl/connectors" -Method Get -ErrorAction Stop
    $connectors = $result.Content | ConvertFrom-Json
    Write-Host "Connectors: $($connectors | ConvertTo-Json)"
} catch {
    Write-Host "Error listing connectors: $_"
}
