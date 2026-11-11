"""ecobot-mcp, the MCP server on its own (docs/ecobot-mcp.md 3.8, step 5): the readings file, the sources and syncers that fill it, the
alert rules that write the alert log, and the MCP server over them (lib/mcp_server.py).

  - `python -m ecobot_mcp` (__main__.py): ecobot-mcp, Streamable HTTP at http://MCP_BIND/mcp;
  - core.py: its startup (the sources, the alert log, the tools, the alert rules, the keep-warm, the first fetch, the archive, the
    backfills and Home Assistant's imports), which ecobot-mcp and the bot (ecobot.py, while it runs everything in-process) both call."""
