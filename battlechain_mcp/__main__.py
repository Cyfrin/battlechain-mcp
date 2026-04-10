import asyncio
from battlechain_mcp.server import main

def run() -> None:
    asyncio.run(main())

if __name__ == "__main__":
    run()
