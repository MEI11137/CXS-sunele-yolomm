"""Local test-only MCP server: no file access and no model/API calls."""
import sys
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP('haixun-integration-echo', host='127.0.0.1', port=int(sys.argv[1]) if len(sys.argv) > 1 else 8000, json_response=True)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(text: str) -> dict:
    """Return the caller's test string."""
    return {'text': text}


if __name__ == '__main__':
    server.run(transport='streamable-http' if len(sys.argv) > 1 else 'stdio')
