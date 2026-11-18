# Snippets to copy-paste individually — this file is not meant to be executed as a script.
#
# Local (stdio):
claude mcp add geo -- geo-mcp serve
#
# Remote (your self-hosted HTTP endpoint):
claude mcp add --transport http geo https://geo-mcp.example.com/mcp --header "Authorization: Bearer $GEO_MCP_TOKEN"
#
# Headless scheduled brief:
claude -p "/mcp__geo__full-analysis local-construction" --allowedTools "mcp__geo__*" > weekly-brief.md
