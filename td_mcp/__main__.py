from dotenv import load_dotenv

# Must run before importing td_mcp.server: several modules read secrets
# (ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, ...) from os.environ at import
# time. The MCP server is launched as a subprocess (see .mcp.json) with
# whatever env the launching shell happened to have — .env in the repo
# root is the reliable path so ingestion tools don't silently no-op.
load_dotenv()

from td_mcp.server import mcp  # noqa: E402


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
