"""After `pip install turnbook` on a fresh machine: the server starts, lists its tools,
saves a memory and finds it again. Run by .github/workflows/install-check.yml.

    python smoke_install.py            # save, then recall
    python smoke_install.py remember   # only save (before an upgrade)
    python smoke_install.py recall     # only recall (after it: the memory must have survived)
"""
import asyncio
import os
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODE = sys.argv[1] if len(sys.argv) > 1 else "both"


async def main():
    env = None
    if not os.environ.get("CI"):
        # run by hand on someone's own PC: never into their memory (a test fact "release day"
        # once sat in a real memory). The same folder for remember and recall.
        home = os.path.join(tempfile.gettempdir(), "turnbook-smoke")
        env = {**os.environ, "TURNBOOK_HOME": home, "GRAPH_MIND_HOME": home}
    server = StdioServerParameters(command=shutil.which("turnbook") or shutil.which("graph-mind"),
                                   args=[], env=env)
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert {"brain_context", "brain_remember", "brain_folder"} <= tools, tools
            if MODE in ("both", "remember"):
                await client.call_tool("brain_remember", {
                    "content": "The release train leaves on Thursdays", "title": "release day",
                    "memory_type": "fact", "source_ref": "install-check"})
            if MODE in ("both", "recall"):
                found = await client.call_tool("brain_recall", {"query": "release train"})
                text = str(found.structured_content or found.content)
                assert "Thursdays" in text, text
            print(f"ok ({MODE}): {len(tools)} tools on Python {sys.version.split()[0]}")


asyncio.run(main())
