from setuptools import setup, find_packages

setup(
    name="battlechain-mcp",
    version="0.1.0",
    description="Claude Desktop MCP server for the BattleChain security demo",
    packages=find_packages(include=["battlechain_mcp*"]),
    package_data={"battlechain_mcp": ["deployments.json"]},
    install_requires=["mcp>=1.0.0"],
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "battlechain-mcp=battlechain_mcp.__main__:run",
        ],
    },
)
