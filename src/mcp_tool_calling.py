import asyncio
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from nobodywho import Chat, Model, SamplerBuilder, SamplerConfig, TokenStream, tool

from mcp import Client, StdioServerParameters

project_root = Path(__file__).resolve().parent.parent
model_path = project_root / "model" / "Qwen3.5-0.8B-BF16.gguf"

OFFICIAL_DOMAIN = "store.nintendo.com.hk"

# Run report: every step is recorded, then written to output/*.md at the end.
STARTED = time.time()
steps: list[dict] = []
tool_calls: list[dict] = []
server_info: dict[str, dict] = {}


def step(who: str, action: str, detail: str = "", tool: str = "") -> None:
    """Record one run step for the report and print it."""
    steps.append({"who": who, "action": action, "detail": detail, "tool": tool})
    print(f"[{who}] {action}" + (f": {detail}" if detail else ""), flush=True)


def server_commands() -> dict[str, list[str]]:
    """The commands that start the two MCP servers."""
    ddg = shutil.which("duckduckgo-mcp-server") or str(Path(sys.executable).with_name("duckduckgo-mcp-server.exe"))
    playwright = shutil.which("playwright-mcp")
    # Browser artifacts (page snapshots, console logs) go to the system temp
    # dir, so the workspace never collects them.
    out_dir = ["--output-dir", str(Path(tempfile.gettempdir()) / "slm-playwright-mcp")]
    return {
        "duckduckgo-mcp-server": [ddg],
        "playwright-mcp": (
            [playwright, "--headless", *out_dir] if playwright
            else ["npx", "-y", "@playwright/mcp@latest", "--headless", *out_dir]
        ),
    }


def display_command(command: list[str]) -> str:
    """The command as shown in the report, with no local paths."""
    parts = [Path(command[0]).stem]
    skip_next = False
    for arg in command[1:]:
        if skip_next:
            skip_next = False
        elif arg == "--output-dir":
            parts.append("--output-dir <system temp>")
            skip_next = True
        else:
            parts.append(arg)
    return " ".join(parts)


_loop = asyncio.new_event_loop()
_sessions: dict[str, Client] = {}
_ready = threading.Event()
_startup_errors: list[str] = []
_server_count = 0


async def _serve(name: str, command: list[str]) -> None:
    """Open one MCP client, keep it alive forever, mark it when all are up."""
    try:
        params = StdioServerParameters(command=command[0], args=command[1:])
        async with Client(params) as client:
            _sessions[name] = client
            _mark_ready()
            await asyncio.Event().wait()  # stay alive
    except Exception as exc:
        _startup_errors.append(f"{name}: {type(exc).__name__}: {exc}")
        _mark_ready()


def _mark_ready() -> None:
    """Set the ready flag once every server is up (or has failed)."""
    if len(_sessions) + len(_startup_errors) >= _server_count:
        _ready.set()


def _run(coro, timeout: float = 60):
    """Run one coroutine on the MCP event loop and wait for its result."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


def start_mcp() -> None:
    """Start both MCP servers; record every tool each one offers."""
    global _server_count
    commands = server_commands()
    _server_count = len(commands)
    threading.Thread(target=_loop.run_forever, daemon=True).start()
    for name, command in commands.items():
        asyncio.run_coroutine_threadsafe(_serve(name, command), _loop)
    if not _ready.wait(90):
        step("harness", "MCP startup failed", "servers did not start within 90 seconds")
        return
    for name, session in _sessions.items():
        tool_names = [t.name for t in _run(session.list_tools()).tools]
        server_info[name] = {"command": display_command(commands[name]), "tools": tool_names}
        step("harness", f"{name} started", ", ".join(tool_names))
    for error in _startup_errors:
        step("harness", "MCP startup error", error)


def mcp_call(server: str, tool: str, arguments: dict, timeout: float = 120) -> str:
    """Call one MCP tool; failures come back as 'ERROR: ...' text."""
    if not _ready.wait(90):
        return "ERROR: MCP servers did not start within 90 seconds."
    session = _sessions.get(server)
    if session is None:
        return f"ERROR: no MCP session for {server} ({'; '.join(_startup_errors)})"
    started = time.time()
    try:
        result = _run(session.call_tool(tool, arguments), timeout)
        output = "\n".join(c.text for c in result.content if getattr(c, "text", None))
        output = "ERROR: " + output if result.is_error else (output or "(no text returned)")
    except Exception as exc:
        output = f"ERROR: {tool} on {server} failed: {type(exc).__name__}: {exc}"
    tool_calls.append({"server": server, "tool": tool, "arguments": arguments,
                       "output": output, "seconds": time.time() - started})
    return output


def search_web(query: str) -> str:
    """DuckDuckGo `search` wrapper; the official domain is always appended."""
    return mcp_call(
        "duckduckgo-mcp-server", "search",
        {"query": f"{query} {OFFICIAL_DOMAIN}", "region": "wt-wt", "max_results": 10},
    )


_EXTRACT_TEXT_JS = (
    "() => (document.body ? document.body.innerText : '')"
    ".replace(/\\s+/g, ' ').trim().slice(0, 4000)"
)


def _playwright_result(text: str) -> str:
    """Extract the value from a Playwright MCP '### Result' response block."""
    match = re.search(r"### Result\n(.*?)\n### Ran", text, re.DOTALL)
    if not match:
        return ""
    value = match.group(1).strip()
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, str) else str(parsed)
    except json.JSONDecodeError:
        return value


def fetch_webpage(url: str) -> str:
    """Read one official store page with the Playwright browser.

    A real browser is needed: plain HTTP fetchers cannot pass the store's
    AWS WAF JavaScript challenge.
    """
    if OFFICIAL_DOMAIN not in url:
        return f"ERROR: only the official Nintendo store ({OFFICIAL_DOMAIN}) may be read: {url}"
    navigation = mcp_call("playwright-mcp", "browser_navigate", {"url": url}, timeout=90)
    if navigation.startswith("ERROR:"):
        return navigation
    last_error = ""
    for _ in range(10):  # the WAF challenge needs a few seconds to pass
        time.sleep(2)
        raw = mcp_call("playwright-mcp", "browser_evaluate", {"function": _EXTRACT_TEXT_JS}, timeout=60)
        if raw.startswith("ERROR:"):
            # The WAF redirect can destroy the page context while it is still
            # navigating; that is transient, so keep polling the same page.
            last_error = raw
            continue
        text = _playwright_result(raw)
        if len(text) > 200:
            return text
    if last_error:
        return last_error
    return f"ERROR: {url} did not show readable text within 20 seconds."


class SLM:
    """A small language model you can talk to with .invoke()."""

    def __init__(
        self,
        model: Model,
        system_prompt: str,
        n_ctx: int,
        max_completion_tokens: int,
        sampler: SamplerConfig | None = None,
        tools: list | None = None,
    ) -> None:
        """Build one chat with the model and keep it ready for .invoke().

        Parameters:
            model: The loaded model file — the "brain" the chat will use.
            system_prompt: The instruction that tells the model how to behave.
            n_ctx: How many tokens the model can keep in memory at once.
            max_completion_tokens: Largest number of tokens one reply may
                have; longer replies are cut off early.
            sampler: How the model picks each next word. None = default.
            tools: The tools the model may call. None = no tools.

        What it does:
            It builds one live chat and stores it as self.chat.
        """
        self.max_completion_tokens = max_completion_tokens
        self.chat = Chat(
            model=model,
            sampler=sampler,
            system_prompt=system_prompt,
            n_ctx=n_ctx,
            tools=tools or [],
        )

    def invoke(self, user_prompt: str) -> str:
        """Send one prompt and return the reply text, stopped past the cap."""
        response: TokenStream = self.chat.ask(user_prompt)

        text = ""
        for token in response:
            text += token
            if len(self.chat.tokenize(text)) - 1 >= self.max_completion_tokens:
                self.chat.stop_generation()
                break
        return text


# Price-like text: the HKD / HK$ / $ markers followed by digits.
PRICE_RE = re.compile(r"(?:HKD|HK\$|\$)\s*[\d,]+")


def extract_price_line(text: str) -> str:
    """The first price mention on a fetched page, as a clean ASCII token.

    Keeps only the price itself ('HKD 3,700'); the page text never reaches
    the SLM.
    """
    match = PRICE_RE.search(text)
    return " ".join(match.group(0).split()) if match else ""


def price_number(price_text: str) -> str:
    """The digits of a captured price, ready for the SLM's add tool.

    A weak model fumbles 'HKD 3,700' -> 3700, so the harness does it:
    'HKD 3,700' becomes '3700'.
    """
    return re.sub(r"[^\d]", "", price_text)


def official_product_urls(search_results: str) -> list[str]:
    """The official store product URLs in a search result, in order, deduped."""
    urls = [
        url
        for url in re.findall(r"https?://[^\s)\]]+", search_results)
        if OFFICIAL_DOMAIN in url and url.rstrip("/") != f"https://{OFFICIAL_DOMAIN}"
    ]
    return list(dict.fromkeys(urls))


def clean_query(line: str) -> str:
    """One line of the SLM's answer, turned into a clean text line."""
    line = re.sub(r"^\s*(?:[-*]|\d+[.)])?\s*", "", line)
    return line.strip().strip('"\'').strip()


def slm_items(query_slm: SLM, question: str, max_items: int = 3) -> list[str]:
    """The SLM's first job: list the products the question asks about.

    A small few-shot example teaches the model to
    output one product name per line for this question.
    """
    answer = query_slm.invoke(
        "Example question: What is the total cost of a PS5 console and a DualSense controller?\n"
        "Example output:\nPS5 console\nDualSense controller\n\n"
        "Now do the same for this question. Output one product per line:\n"
        f"{question}"
    )
    items = list(dict.fromkeys(
        item for item in map(clean_query, answer.splitlines())
        if item and not item.lower().startswith("question")
    ))[:max_items]
    step("slm", "listed the products to buy", "; ".join(items) or "(none)")
    return items


def slm_search_query(query_slm: SLM, item: str, previous: str = "") -> str:
    """The SLM's search job: one short query that finds the item's price page."""
    prompt = (
        "Write one short English web search query that finds the official "
        "Hong Kong price page for this product. "
        "The query must be plain search words, NOT a URL. "
        "Reply with the plain words only.\n\n"
        "Product: PlayStation 5 console\nQuery: PlayStation 5 console price Hong Kong\n\n"
        "Now write the query for this product:\n"
        f"Product: {item}"
    )
    if previous:
        prompt += (
            f"\nYour previous query {previous!r} found no usable price. "
            f"Write a different query for the same product. "
            f"The product name is {item}."
        )
    answer = query_slm.invoke(prompt)
    first = next((line for line in answer.splitlines() if line.strip()), item)
    query = clean_query(first)
    step("slm", "wrote a search query", f"{item} -> {query!r}")
    return query


def find_price(query_slm: SLM, query: str, item: str, attempts: int = 3) -> dict | None:
    """Code-first price hunt for one item; the SLM only writes queries.

    The code searches, picks the official store URLs, reads them with the
    browser and extracts the price. On nothing found, the SLM writes a new
    query and the code retries. Returns {"url", "useful_content"} or None.
    """
    fetch_tools = "MCP tools: `playwright-mcp` -> `browser_navigate`, `browser_evaluate`"
    for attempt in range(1, attempts + 1):
        step("harness", f"search attempt {attempt}", f"{query!r} + {OFFICIAL_DOMAIN}",
             tool="MCP tool: `duckduckgo-mcp-server` -> `search`")
        results = search_web(query)
        urls = official_product_urls(results)
        if not urls:
            step("harness", "no official store URLs in the results",
                 tool="Python function: `official_product_urls()`")
        for url in urls:
            step("harness", "reading page", url, tool=fetch_tools)
            page = fetch_webpage(url)
            if page.startswith("ERROR:"):
                step("harness", "page could not be read", page[:120], tool=fetch_tools)
                continue
            price = extract_price_line(page)
            if price:
                step("harness", "price captured", price,
                     tool="Python function: `extract_price_line()`")
                return {"url": url, "useful_content": price}
            step("harness", "no price mention on this page",
                 tool="Python function: `extract_price_line()`")
        if attempt < attempts:
            query = slm_search_query(query_slm, item, previous=query)
    step("harness", "giving up on this item", "no price captured")
    return None


# The SLM cannot be trusted with arithmetic; Python does the addition.
@tool(
    description="""Adds two HKD amounts and returns the exact total.
    Use this tool to get the total price instead of doing the math yourself.""",
    params={
        "a": "The first price in HKD as a number (for example 100).",
        "b": "The second price in HKD as a number (for example 50).",
    },
)
def add(a: float, b: float) -> str:
    """Add two numbers exactly in Python and return the total."""
    return f"{a + b:g}"


def md_cell(text, limit: int = 300) -> str:
    """One safe Markdown table cell: single-line, pipes escaped, capped."""
    flat = " ".join(str(text).split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[:limit] + "..."


def write_report(path: Path, question: str, price_data: dict, answer: str) -> None:
    """Write the whole run as a Markdown report with step-by-step tables."""
    lines = [
        "# SLM web search run report",
        "",
        f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Model: `{model_path.name}`",
        f"- Run time: {time.time() - STARTED:.0f} seconds",
        f"- Question: {question}",
        "",
        "## Who did what",
        "",
        "- AI agent (SLM): listed the products the question asks about, wrote one "
        "search query per product, wrote replacement queries when a lookup failed, "
        "called the `add` tool (Python function `add()`) for the total, wrote the "
        "final answer.",
        "- Harness (code): started the MCP servers, then ran every lookup itself - "
        "`search_web()` (calls MCP tool `search` of `duckduckgo-mcp-server`), "
        "`fetch_webpage()` (calls MCP tools `browser_navigate` and `browser_evaluate` "
        "of `playwright-mcp`), `official_product_urls()` and `extract_price_line()` "
        "(pure Python). It also assembled the price JSON, tracked every step and "
        "wrote this report.",
        "",
        "## MCP servers and their tools",
        "",
        "| MCP server | Command | All tools offered |",
        "|---|---|---|",
    ]
    for name, info in server_info.items():
        lines.append(f"| {name} | `{md_cell(info['command'])}` | {md_cell(', '.join(info['tools']), 500)} |")
    lines += [
        "",
        "## MCP tool calls made by the harness (code)",
        "",
        "Every call below was executed by the harness itself; the AI agent never "
        "calls MCP tools directly.",
        "",
        "| # | MCP server | Tool | Arguments | Output | Seconds |",
        "|---|---|---|---|---|---|",
    ]
    for index, call in enumerate(tool_calls, 1):
        lines.append(
            f"| {index} | {call['server']} | {call['tool']} "
            f"| `{md_cell(call['arguments'], 150)}` "
            f"| {md_cell(call['output'], 200)} | {call['seconds']:.1f} |"
        )
    lines += [
        "",
        "## Step-by-step timeline",
        "",
        "| # | Who | Step | Tool used | Details |",
        "|---|---|---|---|---|",
    ]
    who = {"harness": "Harness (code)", "slm": "AI agent (SLM)"}
    for index, item in enumerate(steps, 1):
        tool = md_cell(item["tool"]) or "-"
        lines.append(
            f"| {index} | {who[item['who']]} | {item['action']} "
            f"| {tool} | {md_cell(item['detail'])} |"
        )
    lines += [
        "",
        "## Price data found by the code (JSON)",
        "",
        "```json",
        json.dumps(price_data, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Final AI agent reply",
        "",
        answer.strip(),
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    step("harness", "run started", "loading the model and starting the MCP servers")
    start_mcp()
    model = Model(model_path)
    low_temp = SamplerBuilder().temperature(0.1).dist()

    query_slm = SLM(
        model=model,
        system_prompt="Follow the instruction exactly and reply with the requested lines only.",
        n_ctx=8192,
        max_completion_tokens=1000,
        sampler=low_temp,
    )
    answer_slm = SLM(
        model=model,
        system_prompt=(
            "Answer the user's question using only the official price data you are given. "
            "When the question asks for a total, you MUST call the add tool with the "
            "price numbers; NEVER do the math yourself. If a price is missing, say so "
            "honestly and do not invent one."
        ),
        n_ctx=8192,
        max_completion_tokens=1000,
        sampler=low_temp,
        tools=[add],
    )

    user_question = (
        "If I buy the Nintendo Switch 2 console and a Nintendo Switch 2 Pro Controller "
        "in Hong Kong, how much will they cost in total?"
    )
    step("harness", "user question", user_question)

    price_data: dict[str, dict] = {}
    for item in slm_items(query_slm, user_question):
        query = slm_search_query(query_slm, item)
        found = find_price(query_slm, query, item)
        if found:
            price_data[item] = found
    step("harness", "price data assembled", json.dumps(price_data, ensure_ascii=False),
         tool="Python function: `json.dumps()`")

    evidence = "\n".join(
        f"- {item}: {data['useful_content']} "
        f"(number: {price_number(data['useful_content'])}, source: {data['url']})"
        for item, data in price_data.items()
    ) or "Nothing was captured; the web search failed."
    if len(price_data) >= 2:
        final_hint = "Call the add tool now with a and b from the price data above."
    else:
        final_hint = (
            "Do not call the add tool. State the price that was found and say "
            "that the other item's price is missing."
        )

    answer = answer_slm.invoke(
        f"{user_question}\n\n"
        f"Official price data found by the tools:\n{evidence}\n\n{final_hint}"
    )
    for message in answer_slm.chat.get_chat_history():
        for call in message.get("tool_calls") or []:
            fn = call["function"]
            step("slm", "called the add tool", f"{fn['name']} with {fn['arguments']}",
                 tool="Python tool: `add()`")
        if message["role"] == "tool":
            step("slm", "add tool returned", str(message.get("content")),
                 tool="Python tool: `add()`")
    step("slm", "wrote the final answer", answer.strip())

    report_path = project_root / "output" / time.strftime("run_%Y%m%d_%H%M%S.md")
    step("harness", "writing the report", f"output/{report_path.name}",
         tool="Python function: `write_report()`")
    write_report(report_path, user_question, price_data, answer)
    print(f"[done] report saved to {report_path}", flush=True)
