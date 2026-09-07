from mcp.server.fastmcp import FastMCP
from src import storage
from src.watch_officer_agent import build_watch_officer_context

# Initialize the FastMCP server instance
mcp = FastMCP("NAS-Sentinel-Operations")

@mcp.tool()
def get_telemetry_snapshot() -> dict:
    """Retrieve the comprehensive deterministic operational telemetry snapshot including airport feed health and phase counts."""
    return build_watch_officer_context()

@mcp.tool()
def get_active_alerts(limit: int = 50, severity: str | None = None) -> list[dict]:
    """Retrieve recent operational alerts with optional severity filtering (e.g., CRITICAL, WARNING)."""
    alerts = storage.get_recent_alerts(limit=limit)
    if severity:
        return [a for a in alerts if a.get("severity") == severity.upper()]
    return alerts

@mcp.tool()
def get_recent_events(limit: int = 100) -> list[dict]:
    """Retrieve recent raw FAA SWIM/STDDS system events from the storage layer."""
    return storage.get_recent_events(limit=limit)

if __name__ == "__main__":
    mcp.run()