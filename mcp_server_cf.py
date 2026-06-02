"""
mcp_server_cf.py
----------------
Cloud Foundry entrypoint for the AVA MCP server.

CF injects the PORT environment variable automatically.
The server binds to 0.0.0.0 (all interfaces) so CF's router can reach it.
"""
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from starlette.requests import Request
from starlette.responses import FileResponse, Response

from server.server import mcp

ICON_PATH = pathlib.Path(__file__).parent / "data" / "images" / "icon.ico"


@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon(request: Request) -> Response:
    if ICON_PATH.exists():
        return FileResponse(ICON_PATH, media_type="image/x-icon")
    return Response(status_code=404)


port = int(os.environ.get("PORT", 8080))

mcp.settings.host = "0.0.0.0"
mcp.settings.port = port
mcp.run(transport="streamable-http")
