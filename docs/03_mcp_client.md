# Chapter 3 — Connecting to MCP servers (`mcp_client.py`)

> **Part of the [study guide](./study_guide.md). Previous: [Chapter 2 — Tool calling](./02_basic_tool_calling.md) · Next: [Chapter 4 — The full pipeline](./04_mcp_tool_calling.md)**

Until now the "tools" were our own Python functions with fake data. This chapter connects to the **MCP ecosystem**: ready-made servers that give your programs real capabilities — web search, page reading, browser control — without you writing and maintaining scraping code.

This chapter's program is small, but it checks that all the working parts of the final project fit together. It connects to both MCP servers and prints the tools each one offers.

```
src/mcp_client.py
```

---

## 3.1 What is MCP?

**Model Context Protocol (MCP)** is an open protocol (created by Anthropic, now widely adopted) that standardizes how AI applications talk to external tools and data. The shape is always the same:

- An **MCP server** is a program that offers abilities — it provides a list of **tools**, each with a name and a description of what inputs it accepts.
- An **MCP client** is your code. It starts the server (as a separate process, talking over "stdio" — the process's standard text input/output channels), lists its tools, and calls them.

Read more:

- Official site & docs: https://modelcontextprotocol.io/
- Official "Build an MCP client" tutorial (the one this file is based on): https://modelcontextprotocol.io/docs/develop/build-client
- Python SDK: https://github.com/modelcontextprotocol/python-sdk

**Why this project exists:** instead of hand-writing web search + page scraping (login walls, JavaScript challenges, anti-bot defenses… a never-ending maintenance job), we pick existing MCP servers from the ecosystem, combine them, and get a **free web-search pipeline** to power a local SLM.

---

## 3.2 The two servers

| MCP server | Language / install | What it gives us |
|---|---|---|
| `duckduckgo-mcp-server` | Python (pip; lives in `.venv`) · [GitHub](https://github.com/nickclyde/duckduckgo-mcp-server) · [PyPI](https://pypi.org/project/duckduckgo-mcp-server/) | Free web search (`search`), page fetching (`fetch_content`), URL expanding (`expand_link`) — no API key needed |
| `playwright-mcp` | Node.js (npm global) · [GitHub](https://github.com/microsoft/playwright-mcp) · [npm](https://www.npmjs.com/package/@playwright/mcp) | A real browser: `browser_navigate`, `browser_evaluate`, `browser_click`, … 25 tools |

Two servers, two ecosystems — that's the power of MCP: the client doesn't care what a server is written in.

---

## 3.3 The code, explained

### Finding the launch commands

```python
def server_commands() -> dict[str, list[str]]:
    """The commands that start the two MCP servers."""
    ddg = shutil.which("duckduckgo-mcp-server") or str(Path(sys.executable).with_name("duckduckgo-mcp-server.exe"))
    playwright = shutil.which("playwright-mcp")
    # Browser artifacts go to the system temp dir, so the workspace stays clean.
    out_dir = ["--output-dir", str(Path(tempfile.gettempdir()) / "slm-playwright-mcp")]
    return {
        "duckduckgo-mcp-server": [ddg],
        "playwright-mcp": (
            [playwright, "--headless", *out_dir] if playwright
            else ["npx", "-y", "@playwright/mcp@latest", "--headless", *out_dir]
        ),
    }
```

Details that matter:

- **`shutil.which("duckduckgo-mcp-server")`** looks for the command on PATH (the list of folders where your system finds programs). When running inside the venv it is found there; the `Path(sys.executable).with_name(...)` fallback points to the program next to the running Python — a reliable way to find it even if PATH is unusual.
- **`playwright-mcp`** was installed with `npm install -g`. If it is missing, we fall back to `npx -y @playwright/mcp@latest` — npx downloads and runs it right away (slower the first time, but always works).
- **`--headless`** = run the browser without showing a window.
- **`--output-dir <temp>`** — Playwright MCP writes page snapshots and console logs for each navigation. By default that lands in a `.playwright-mcp/` folder in the current directory, which fills your project with junk files after every run (we once collected 51 files!). We point it at the **system temp directory** instead, so the workspace stays clean. (It is also in `.gitignore` as extra safety.)

### Connecting and listing tools

```python
async def show_tools(name: str, command: list[str]) -> None:
    """Connect to one MCP server and print the tools it offers."""
    params = StdioServerParameters(command=command[0], args=command[1:])
    try:
        async with Client(params) as client:
            result = await client.list_tools()
    except Exception as exc:
        print(f"[mcp] {name} FAILED to start: {type(exc).__name__}: {exc}")
        return
    print(f"[mcp] {name} started, tools exposed:")
    for tool in result.tools:
        print(f"  - {tool.name}")
```

- `StdioServerParameters` describes **how to launch** the server: the command and its arguments.
- **`async with Client(params)`** is the whole life of the connection, from start to end. Entering it launches the server process, does the MCP introduction handshake, and prepares the session; leaving it disconnects and shuts the server down. Nothing to close by hand.
  - The official tutorial writes `Client(stdio_client(params))`; in `mcp` 2.3+ the constructor also accepts `StdioServerParameters` directly, which is what we use here.
- **`client.list_tools()`** returns the server's tool list. Each tool has `.name` (and `.input_schema` describing its arguments — try printing it in the exercises).

```python
async def main() -> None:
    """Start each MCP server in turn and show its tools."""
    for name, command in server_commands().items():
        await show_tools(name, command)
```

Both servers are started **one after the other**, each in its own clean `async with` block.

---

## 3.4 Run it

```powershell
uv run --python ".venv" python -X utf8 src/mcp_client.py
```

Expected output (from a real run on this machine):

![mcp client output](../screenshot/mcp_client.png)

- `duckduckgo-mcp-server` → `search`, `fetch_content`, `expand_link`
- `playwright-mcp` → 25 `browser_*` tools

You will also see the DuckDuckGo server print its own configuration banner (rate limits like `search=30/min`, cache TTL, etc.) on stderr — that's normal.

---

## 3.5 The discovery that shaped the project: `fetch_content` is not enough

The DuckDuckGo server can also **fetch pages** (`fetch_content`) — so why do we need a whole browser server at all? Here is what we found while building this project:

1. **The target pages are protected by AWS WAF.** The official Nintendo Hong Kong store (`store.nintendo.com.hk`) sits behind an AWS WAF **JavaScript challenge**: a plain HTTP request gets an HTTP 202 response with an `x-amzn-waf-action: challenge` header and **zero readable content**. Only a real browser that executes JavaScript can pass the challenge and see the real page.
2. **`fetch_content` is a plain fetcher.** It cannot run JavaScript, so on such pages it returns the challenge page — basically zero useful text. We checked this directly: plain fetching gave empty/blocked responses while a real browser showed the complete page with prices.
3. We also tried other MCP fetch servers from the ecosystem (e.g. headless-fetch-style servers). They failed the same way, or worse — some send custom HTTP headers, which makes WAF block them even harder.

**End result:** for protected sites, no HTTP-based fetcher works. The combination that does work is:

> **`search` (DuckDuckGo MCP) to find URLs + `browser_navigate` + `browser_evaluate` (Playwright MCP) to read the pages.**

This is exactly the "combine ecosystem tools" idea of the project: each server covers the other's weakness. And if one day you target *plain* sites only, `fetch_content` alone would be enough — it's the WAF-protected ones that need the browser.

### Two more ddg-server details you should know

- **`search` rate limit** is 30 searches/minute (the server prints it at startup). Our pipeline uses very few searches per run, so we never hit it.
- **Long URLs get shortened** in search results: URLs longer than 120 characters become `ref://...` tokens (to save context tokens). The server's `expand_link` tool turns them back into real URLs. In our runs, the store URLs were short enough that this never triggered — but if you see a `ref://` in results, remember `expand_link`.

---

## 3.6 Experiments

1. **Print schemas.** Inside `show_tools`, print `tool.input_schema` for each tool. Watch how `browser_navigate` declares a `url` parameter and `search` declares `query` / `max_results` / `region`.
2. **Watch `fetch_content` fail.** Write a tiny script that starts the ddg server and calls `fetch_content` on `https://store.nintendo.com.hk/purchase-switch2`, then on any normal site (for example `https://example.com`). The first returns a challenge/blocked page; the second works fine. You just reproduced the discovery that motivated the whole browser combo.
3. **Call the search tool manually.** Call `search` with `{"query": "Nintendo Switch 2 console price Hong Kong store.nintendo.com.hk", "max_results": 10}` and read the formatted results — the exact format the final pipeline reads.

---

## 3.7 What you learned / where this goes

- MCP clients launch servers over stdio and get a list of tools — no matter what language each server is written in.
- `async with Client(params)` covers the whole life of the connection; `list_tools()` shows what a server can do.
- **Why a browser server is needed:** WAF-protected sites block every plain fetcher; the DuckDuckGo `fetch_content` tool is a plain fetcher, so it can never work on such pages.
- Clean-up tricks: `--output-dir` to temp, npx as the backup option, `shutil.which` lookup.

Next — the finale: make **code** drive these MCP tools step by step while a **local SLM** does only the tiny text jobs it is actually good at: **[Chapter 4 — The full pipeline](./04_mcp_tool_calling.md)**.
