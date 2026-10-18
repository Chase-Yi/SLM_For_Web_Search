# Chapter 4 — The full pipeline (`mcp_tool_calling.py`)

> **Part of the [study guide](./study_guide.md). Previous: [Chapter 3 — MCP client](./03_mcp_client.md) · Next: [Chapter 5 — Troubleshooting](./05_troubleshooting.md)**

This is the finale. One user question goes in:

> *"If I buy the Nintendo Switch 2 console and a Nintendo Switch 2 Pro Controller in Hong Kong, how much will they cost in total?"*

…and this comes out:

> **The total cost is HKD 4238.**

…along with a complete Markdown report full of tables, covering everything that happened, saved to `output/run_<timestamp>.md`.

In this chapter you will build and understand the whole thing: **two MCP servers + a local SLM + a code-first harness** that replaces the "LLM searches the web and answers" experience entirely on your own machine, at zero cost.

```
src/mcp_tool_calling.py
```

---

## 4.1 The design philosophy — and why

The first idea anyone would try: give the SLM the MCP tools and let it drive. We tried variations of that, and the small model failed again and again (details in [4.6](#46-the-small-model-limitation-story)). The architecture that actually works splits the work like this:

| Job | Who does it | Why |
|---|---|---|
| Start/stop MCP servers, list tools | **Code (harness)** | Simple, repeatable code work |
| Write a search query per product | **SLM** | Natural-language task, it's good at this |
| Search, filter official URLs, browse pages, extract prices | **Code (harness)** | Must be exact, easy to test and debug |
| Retry with a new query when a lookup fails | **Code drives, SLM writes the new query** | Best of both |
| Compute the total price | **Code**, invoked by the SLM as an *addition tool* | 2B models cannot do arithmetic (verified) |
| Write the final sentence | **SLM** | Natural-language task |
| Record every step → Markdown report | **Code (harness)** | Repeatable |

The SLM is limited to **three small text jobs**: list the products, write search queries, write the final answer (using the *addition tool* for the math). Everything else is forced by code. This is the *code-first harness* pattern.

The result pipeline:

```mermaid
sequenceDiagram
    participant U as User
    participant H as Harness (code)
    participant S as SLM (local)
    participant D as ddg MCP server
    participant P as playwright MCP server

    U->>H: one question
    H->>S: "list the products"
    S-->>H: console; Pro Controller
    loop each product
        H->>S: "write a search query"
        S-->>H: query text
        H->>D: search(query + official domain)
        D-->>H: search results
        H->>H: keep only official store URLs
        H->>P: browser_navigate(official URL)
        H->>P: browser_evaluate(page text)
        P-->>H: page text
        H->>H: extract price with a text pattern (regex)
        alt no price found
            H->>S: "write a different query" (retry, max 3)
        end
    end
    H->>H: assemble {"url", "useful_content"} JSON
    H->>S: question + price data + final hint
    S->>H: addition tool call (uses Python add())
    S-->>H: "The total cost is HKD 4238."
    H->>H: write output/run_<timestamp>.md
```

---

## 4.2 Part 1 — Recording what happens (for the report)

Everything the report needs is written down while the run happens. Top of the file:

```python
# Run report: every step is recorded, then written to output/*.md at the end.
STARTED = time.time()
steps: list[dict] = []
tool_calls: list[dict] = []
server_info: dict[str, dict] = {}


def step(who: str, action: str, detail: str = "", tool: str = "") -> None:
    """Record one run step for the report and print it."""
    steps.append({"who": who, "action": action, "detail": detail, "tool": tool})
    print(f"[{who}] {action}" + (f": {detail}" if detail else ""), flush=True)
```

- `who` is `"harness"` or `"slm"` — this is what later becomes the **"Who"** column in the report, making the split between code and the SLM clear.
- `tool` names exactly what executed the step: an MCP tool (`"MCP tool: duckduckgo-mcp-server -> search"`), a Python function (`"Python function: extract_price_line()"`), or the SLM's tool (`"Python tool: add()"`).
- Every call prints immediately (`flush=True`) so you can watch progress live.

---

## 4.3 Part 2 — Server commands and the MCP core

`server_commands()` and `display_command()` are the same ideas as [Chapter 3](./03_mcp_client.md), plus hiding file paths for the report:

```python
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
```

(`Path(...).stem` turns `C:\...\node\playwright-mcp.CMD` into just `playwright-mcp`; the temp path is replaced by `<system temp>` — the report must never contain personal local paths.)

### Keeping MCP sessions alive in the background

Here is the one truly difficult part of the whole project. The SLM runs **synchronously** — meaning each call waits and blocks everything else while it runs. But MCP clients are **async** — they are built to wait for many things at the same time. The solution: run one asyncio event loop in a **background thread** (a daemon thread: one that closes automatically when the program ends), keep both sessions open there forever, and let the main code call into it through a bridge that is safe to use from two threads at once.

```python
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
```

- Each server gets a `_serve` coroutine (an async function that can wait for other things while it runs) that connects and then **waits forever on an empty `asyncio.Event()`** — that keeps the `async with` block (and therefore the session!) alive for the whole program.
- `_mark_ready()` sets a threading.Event once both servers are up (or have failed).

```python
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
```

- `_loop.run_forever` runs the event loop in the daemon thread.
- `asyncio.run_coroutine_threadsafe(coro, _loop)` sends work to that loop **from the main thread**.
- After the `_ready` event is set, we list each server's tools once — saving them in `server_info` (for the report) and in `steps` (for the live log).

```python
def _run(coro, timeout: float = 60):
    """Run one coroutine on the MCP event loop and wait for its result."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)
```

And the bridge used by all tool calls:

```python
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
```

- Returns **text, never throws an error** — a failure becomes an `"ERROR: ..."` string, and every call is timed and added to `tool_calls` (the raw material for the report's call table).
- Note how `call_tool(tool, arguments)` takes the same inputs as `Client.call_tool(name, arguments)` — the same call shape also works with the more basic `ClientSession` API.

---

## 4.4 Part 3 — The two wrapped tools

### `search_web` — the DuckDuckGo `search` tool

```python
def search_web(query: str) -> str:
    """DuckDuckGo `search` wrapper; the official domain is always appended."""
    return mcp_call(
        "duckduckgo-mcp-server", "search",
        {"query": f"{query} {OFFICIAL_DOMAIN}", "region": "wt-wt", "max_results": 10},
    )
```

- **`OFFICIAL_DOMAIN = "store.nintendo.com.hk"`** is added to every query — the code guarantees we search for official-store results, no matter what the SLM writes.
- `region: "wt-wt"` = no region preference; `max_results: 10`.
- This is the **first of the two "free web search" building blocks**. No API key, no cost.

### `fetch_webpage` — the Playwright browser

```python
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
    for _ in range(10):  # the WAF challenge needs a few seconds to pass
        time.sleep(2)
        raw = mcp_call("playwright-mcp", "browser_evaluate", {"function": _EXTRACT_TEXT_JS}, timeout=60)
        if raw.startswith("ERROR:"):
            return raw
        text = _playwright_result(raw)
        if len(text) > 200:
            return text
    return f"ERROR: {url} did not show readable text within 20 seconds."
```

- **Domain check:** only official-store URLs may be read — the SLM can never send the browser to any other site.
- It navigates, then **keeps asking for the page text again and again**: the WAF challenge takes a few seconds to pass, so we ask up to 10 times (every 2 s) and take the first text longer than 200 characters.
- The text comes from JavaScript run inside the page:

```python
_EXTRACT_TEXT_JS = (
    "() => (document.body ? document.body.innerText : '')"
    ".replace(/\\s+/g, ' ').trim().slice(0, 4000)"
)
```

(All extra whitespace turned into single spaces, cut off at 4000 characters — the full page content is never handed to the SLM, but we keep it bounded anyway.)

And because Playwright MCP wraps its answers inside a special text block with `### Result` / `### Ran` markers, a tiny parser takes the value out of it:

```python
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
```

This is the **second building block**: a real browser reading real pages — the part plain HTTP fetchers can never do ([Chapter 3, 3.5](./03_mcp_client.md#35-the-discovery-that-shaped-the-project-fetch_content-is-not-enough)).

---

## 4.5 Part 4 — Price extraction, all in code

```python
# Price-like text: the HKD / HK$ / $ markers followed by digits.
PRICE_RE = re.compile(r"(?:HKD|HK\$|\$)\s*[\d,]+")


def extract_price_line(text: str) -> str:
    """The first price mention on a fetched page, as a clean ASCII text.

    Keeps only the price itself ('HKD 3,700'); the page text never reaches
    the SLM.
    """
    match = PRICE_RE.search(text)
    return " ".join(match.group(0).split()) if match else ""
```

Why this shape? We find the price with a **regex** — a text pattern (explained in the study guide glossary) that here means "the text `HKD` followed by digits". Verified by testing directly on the store page:

- The page marks its price with the plain text **`HKD`** (ASCII — English letters and digits; we counted: `HKD 3,700` and `HKD3,700`, and zero times for `HK$`, `$`-with-digits, or Chinese price words like 港幣/價格/售價).
- **The first match is used** — the product's own price appears before the prices of accessories in the visible text.
- Only the tiny text (`"HKD 3,700"`) is kept. The page text never reaches the SLM.
  - An earlier version passed a 70-character *piece* around the price to the SLM. The piece contained Chinese characters; the model then made up duplicated lines. **Lesson: give the SLM clean ASCII text, not pieces of the page.**
- The URL filter keeps only official product pages, in order, with duplicates removed:

```python
def official_product_urls(search_results: str) -> list[str]:
    """The official store product URLs in a search result, in order, deduped."""
    urls = [
        url
        for url in re.findall(r"https?://[^\s)\]]+", search_results)
        if OFFICIAL_DOMAIN in url and url.rstrip("/") != f"https://{OFFICIAL_DOMAIN}"
    ]
    return list(dict.fromkeys(urls))
```

(Search result pages can link the store's front page too; `rstrip("/")` excludes that root URL so we only visit *product* pages.)

---

## 4.6 The small-model limitation story

This section documents the real debugging journey. Each stage looked like a good idea; each failed; each fix taught a rule about small models.

### (a) Let the model do the arithmetic → failed

The model was asked to add the two prices by itself. Results, verified with many prompt variants:

- `3700 + 538` → correct (`4238`) — sometimes.
- `2150 + 449` → answered **`2150`**, the first number, *every single time* (plain question, "plus" wording, "number only", step-by-step…).
- When the prompt contained a price list with brand words, the model repeated the first price instead of adding anything.

**Rule learned:** a 2B model cannot reliably do arithmetic. **Fix:** an *addition tool* — the SLM *calls* it, Python computes:

```python
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
```

Verified from start to finish: the SLM called `add({'a': 3700, 'b': 538})` → `4238` → "The total cost is HKD 4238."

### (b) Asking the model to "list the products" → it listed ONE

Prompt: *"list every product the user wants to buy, one product per line."* The model returned only **`Nintendo Switch 2 Pro Controller`** — it only paid attention to the product mentioned *last*, and missed the console completely (so the pipeline then searched for only one item!).

**Fix:** an **example** built right into the prompt (this technique is called "few-shot"):

```python
    answer = query_slm.invoke(
        "Example question: What is the total cost of a PS5 console and a DualSense controller?\n"
        "Example output:\nPS5 console\nDualSense controller\n\n"
        "Now do the same for this question. Output one product per line:\n"
        f"{question}"
    )
```

An example with *different* products (PS5!) showing exactly two output lines fixed it: 3 out of 3 runs produced both products, and the example products never appeared in the answer. **Rule learned:** for format tasks, one good example works better than any number of written instructions.

### (c) The system prompt was pushing the model the wrong way

The item-list job failed even with the example — because the SLM's system prompt said *"You write short web search queries. Reply with the query text only."* That instruction spilled over into the *listing* task and pushed it to output a single line.

**Fix:** a neutral system prompt that works for both jobs:

```python
        system_prompt="Follow the instruction exactly and reply with the requested lines only.",
```

Verified: with this prompt, both the listing job and the query-writing job produce correct, clean output. **Rule learned:** one SLM instance with two jobs needs a system prompt that is neutral between them.

### (d) One missing price should not produce a wrong total

If only one lookup succeeds, asking the model to "add the prices" becomes a problem (we watched it add the same price twice). **Fix:** the final hint is chosen **by code**, based on how many prices were really found:

```python
    if len(price_data) >= 2:
        final_hint = (
            "Call the add tool with the prices to get the total, "
            "then answer in one sentence: 'The total cost is HKD [total]'."
        )
    else:
        final_hint = (
            "Do not call the add tool. State the price that was found and say "
            "that the other item's price is missing."
        )
```

Verified in a run where only one price was found: the answer became *"The price for the Nintendo Switch 2 Pro Controller is HKD 538. The price for the Nintendo Switch 2 console is missing."* — honest, no made-up math. **Rule learned:** let code decide which instruction the model should receive, based on hard facts the code knows.

### The takeaway

> A small model only transforms text — it is not a self-working agent that plans and finishes the whole task alone. It can write a query, list two product names, and phrase one sentence. Everything else — searching, filtering, browsing, extracting, retrying, adding, reporting — must be **forced by code**. That is why this project is a *harness* around an SLM, not a fully independent agent.

---

## 4.7 Part 5 — The harness functions that drive everything

### The retry loop: `find_price`
```python
def find_price(query_slm: SLM, query: str, item: str, attempts: int = 3) -> dict | None:
    """Code-first price hunt for one item; the SLM only writes queries."""
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
```

Every interesting action reports through `step(...)` with the tool that did it — this is where the report's timeline columns come from. Retries only happen when needed; the SLM writes a *different* query each retry:

```python
def slm_search_query(query_slm: SLM, item: str, previous: str = "") -> str:
    """The SLM's search job: one short query that finds the item's price page."""
    prompt = (
        "Write one short English web search query that finds the official "
        f"Hong Kong price page for this product. Reply with the query only.\n\nProduct: {item}"
    )
    if previous:
        prompt += f"\nThe previous query {previous!r} found no usable price; write a different one."
    answer = query_slm.invoke(prompt)
    first = next((line for line in answer.splitlines() if line.strip()), item)
    query = clean_query(first)
    step("slm", "wrote a search query", f"{item} -> {query!r}")
    return query
```

(`clean_query` removes list markers (-, *, 1.) and quote marks, so extra text from the model cannot break the query.)

### The main flow

```python
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
            "When the question asks for a total, call the add tool with the prices; never "
            "do the math yourself. Be brief and exact; if a price is missing, say so honestly."
        ),
        n_ctx=8192,
        max_completion_tokens=1000,
        sampler=low_temp,
        tools=[add],
    )
```

Note that the two chats are different on purpose:

- `query_slm` has **no tools** and a small context (`8192`) — it only writes lines of text.
- `answer_slm` has **the *addition tool*** and no JSON schema — tool calling needs the default sampler ([Chapter 2, 2.3](./02_basic_tool_calling.md#23-the-two-stage-pattern-and-a-critical-conflict)).
- Both have a limit of `max_completion_tokens=1000`.

```python
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
```

The harness goes through whatever the SLM listed; for each item: query → search → fetch → extract → save `{"url", "useful_content"}`.

```python
    evidence = "\n".join(
        f"- {item}: {data['useful_content']} (source: {data['url']})"
        for item, data in price_data.items()
    ) or "Nothing was captured; the web search failed."
```

The evidence given to the SLM is **only the clean, code-extracted data** — no page text, ever.

Then the dynamic final hint (see 4.6-d), the invoke, and after it:
```python
    for message in answer_slm.chat.get_chat_history():
        for call in message.get("tool_calls") or []:
            fn = call["function"]
            step("slm", "called the add tool", f"{fn['name']} with {fn['arguments']}",
                 tool="Python tool: `add()`")
        if message["role"] == "tool":
            step("slm", "add tool returned", str(message.get("content")),
                 tool="Python tool: `add()`")
    step("slm", "wrote the final answer", answer.strip())
```

We look at the answer chat's history so the report shows the **actual tool call** (`add with {'a': 3700, 'b': 538}`) and its result (`4238`) as full steps of their own.

---

## 4.8 Part 6 — Writing the report

```python
def md_cell(text, limit: int = 300) -> str:
    """One safe Markdown table cell: single-line, pipes escaped, capped."""
    flat = " ".join(str(text).split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[:limit] + "..."
```

Markdown tables break on multi-line text or on a `|` inside a cell; `md_cell` turns the text into one line, protects the special characters, and cuts it to a length limit.

`write_report()` then writes six sections into `output/run_<timestamp>.md`:

1. **Header** — timestamp, model name, run time, the question.
2. **Who did what** — the SLM jobs vs the harness jobs (the design, written into the report itself).
3. **MCP servers and their tools** — each server's command (paths hidden) and its full tool list.
4. **MCP tool calls made by the harness** — numbered table of every call: server, tool, arguments, output (cut to a length limit), seconds. It clearly says that *the harness* executes these; the SLM never calls MCP directly.
5. **Step-by-step timeline** — every `step()` entry with **Who / Step / Tool used / Details**.
6. **Price data JSON + final reply** — the data put together by code and the model's final sentence.

---

## 4.9 Run it — and reproduce the output

Clean up any leftover processes from earlier experiments, then run:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "playwright-mcp|duckduckgo-mcp-server|headless_shell|npx" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
uv run --python ".venv" python -X utf8 -u src/mcp_tool_calling.py
```

Live output (printed as it happens):

```
[harness] run started: loading the model and starting the MCP servers
[harness] duckduckgo-mcp-server started: search, fetch_content, expand_link
[harness] playwright-mcp started: browser_close, browser_resize, ...
[harness] user question: If I buy the Nintendo Switch 2 console and ...
[slm] listed the products to buy: Nintendo Switch 2 console; Nintendo Switch 2 Pro Controller
[slm] wrote a search query: Nintendo Switch 2 console -> 'Nintendo Switch 2 console price Hong Kong'
[harness] search attempt 1: 'Nintendo Switch 2 console price Hong Kong' + store.nintendo.com.hk
[harness] reading page: https://store.nintendo.com.hk/purchase-switch2
[harness] price captured: HKD 3,700
[slm] wrote a search query: Nintendo Switch 2 Pro Controller -> 'Nintendo Switch 2 Pro Controller price Hong Kong'
[harness] reading page: https://store.nintendo.com.hk/00000000000069
[harness] price captured: HKD 538
[harness] price data assembled: {"Nintendo Switch 2 console": {...}, "Nintendo Switch 2 Pro Controller": {...}}
[slm] called the add tool: add with {'a': 3700, 'b': 538}
[slm] add tool returned: 4238
[slm] wrote the final answer: The total cost is HKD 4238.
[harness] writing the report: output/run_20261018_152058.md
[done] report saved to .../output/run_20261018_152058.md
```

A full run takes **about 15–20 seconds** on this machine (Vulkan GPU; on CPU only it is slower but still works). The result file `output/run_<timestamp>.md` is the thing you just learned to build. Its timeline (18 steps) mixes `Harness (code)` and `AI agent (SLM)` rows together, exactly matching the `step()` calls in the code — compare the two side by side.

### Reproduce checklist

- [ ] The environment from [Chapter 0](./00_environment_setup.md) is fully checked
- [ ] `output/` folder exists (the script creates it if missing)
- [ ] Run command uses `-X utf8` and `-u`
- [ ] Watch the live log: you should see both servers start, two queries, two prices, one `add` call
- [ ] Result: **"The total cost is HKD 4238."** and a fresh `output/run_*.md`

If prices differ on the day you run it — that's fine, the store may have changed them; what this project delivers is the *pipeline*.

---

## 4.10 Exercises

1. **Different question.** Change `user_question` to ask about a different pair of products (e.g. "a Nintendo Switch 2 Pro Controller and a Joy-Con 2 pair"). Watch the SLM list different products and write different queries — nothing is hardcoded in the pipeline.
2. **Third product.** The SLM lists up to 3 items; try a question with three products. Note the dynamic hint: with 3 prices the *addition tool* takes two — think about how you would extend it to many (hint: a `sum` tool).
3. **Another tool.** Add a `multiply` tool and a question about buying 2 controllers. The model will call the right one — tool descriptions drive selection.
4. **Break a lookup on purpose.** Edit `OFFICIAL_DOMAIN` to something wrong (e.g. `store.nintendo.example`) and run. Watch the retry loop (3 attempts) at work and the honest "missing price" final answer.
5. **Read your report carefully.** Open the generated md and question every line — check it against what you know the code does.

---

## 4.11 What you learned / where this goes

- A working **free, local "search + read + answer" pipeline**: DuckDuckGo MCP (search) + Playwright MCP (browser) + regex extraction + a local 2B SLM for the tiny text jobs — zero API keys, zero cost.
- The **code-first harness** pattern: the SLM does 3 text jobs; code does everything else.
- Real proof of small-model limits (arithmetic, attention, prompt effects) and the fixes (tools, examples, neutral prompts, hints chosen by code).
- A complete record system: every step labeled with who did it, timed, and written into the Markdown report.

Appendix: all the traps we hit and how to escape them — **[Chapter 5 — Troubleshooting](./05_troubleshooting.md)**.
