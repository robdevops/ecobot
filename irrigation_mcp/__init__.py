"""irrigation-mcp, the irrigation controller's MCP server (docs/ecobot-mcp.md 3.9, step 8): `python -m irrigation_mcp` (__main__.py)
runs the controller's watchdog and daily check and serves its tools and irrigation://alerts at http://IRRIGATION_BIND/mcp. Its code is
lib/irrigation (the Tuya client, the watchdog, the check, its state, its client of ecobot-mcp, the server); it imports nothing of
ecobot-mcp's sources or tools and nothing of the bot."""
