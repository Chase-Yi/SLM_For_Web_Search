# Chapter 5 — Troubleshooting & Lessons Learned

> **Part of the [study guide](./study_guide.md). Previous: [Chapter 4 — The full pipeline](./04_mcp_tool_calling.md)**

Every problem below was actually hit and solved while building this project. Each entry has three parts: **symptom (what you see) → cause (why it happens) → fix**.

---

## 5.1 Encoding crashes on Windows (`UnicodeEncodeError`, GBK)

**Symptom:** the script crashes with `UnicodeEncodeError: 'gbk' codec can't encode character ...` — often only when output is redirected to a file, and often while printing something unrelated to the crash message.

**Cause:** on a Chinese Windows system, the default text encoding is called GBK. When the program's output is **redirected** to a file (e.g. `> log.txt` — this applies to both output streams: *stdout*, the normal output, and *stderr*, the error output), Python writes it using GBK — and any character GBK has no code for (emoji, some Chinese/Japanese/Korean characters) causes the error and kills the script. Interactive consoles usually use UTF-8, so the same program may work when you watch it and die when you redirect it.

**Fix:** always run Python with `-X utf8`:

```powershell
uv run --python ".venv" python -X utf8 -u src/mcp_tool_calling.py
```

`-X utf8` forces UTF-8 output everywhere. (The alternative — `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at the top of the script — works the same way; this project chose the `-X utf8` route so the code stays short. **What this means in practice: always include `-X utf8` in your run commands.** A design rule that came from this bug: console output in this project is on purpose ASCII-only (plain English letters, digits, and punctuation).)

---

## 5.2 GPU memory exhaustion (`ErrorOutOfDeviceMemory`)

**Symptom:** `ggml_vulkan: vk::Device::allocateMemory: ErrorOutOfDeviceMemory` / `Could not create context: null reference from llama.cpp`, typically after the program ran fine once and then crashes with an out-of-memory error.

**Cause:** each `Chat` context with a large `n_ctx` (e.g. 100000) uses hundreds of MB of graphics card memory. An old version of the project created a **fresh Chat in every loop iteration** — the first iteration worked, the second one ran out of memory.

**Fix:** create each chat **once** and reuse it. Between turns, call `chat.reset_history()` instead of building a new chat. This project also uses small contexts (`n_ctx=8192`) for its helper chats. If you have no GPU at all, the model runs on the CPU (slower, no graphics memory involved) — that is exactly why the small compressed model was chosen.

---

## 5.3 The `.playwright-mcp/` folder appearing in your project

**Symptom:** after runs, a `.playwright-mcp/` folder appears in the project root containing `page-*.yml` snapshots and `console-*.log` files (we reached 51 files / ~740 KB).

**Cause:** Playwright MCP writes files for each navigation to the *current working directory* by default.

**Fix:** pass `--output-dir` pointing at the system temp folder — exactly what `server_commands()` does:

```python
    out_dir = ["--output-dir", str(Path(tempfile.gettempdir()) / "slm-playwright-mcp")]
```

The folder is now created in temp (eventually cleaned by the OS), and the project stays clean. `.gitignore` also lists `.playwright-mcp/` as extra safety. The folder is **always safe to delete by hand**.

---

## 5.4 `playwright-mcp` / `npx` not found

**Symptom:** `[mcp] ERROR: no MCP session for playwright-mcp` at startup, or the `playwright-mcp` command missing.

**Cause:** the Node package was never installed, or a new terminal doesn't see the npm global bin path.

**Fix:**

```powershell
npm install -g @playwright/mcp
playwright-mcp --version
```

If Node itself is missing: install Node.js LTS from https://nodejs.org/ first. The code also has an `npx -y @playwright/mcp@latest` fallback that downloads and runs the server right away — the **first run with npx is slow** (it downloads the package), and on some office networks npm downloads are blocked. Prefer the global install.

**Missing browser:** Playwright MCP normally needs **no browser install** — it uses its own Chromium, downloaded automatically on first use (into `%LOCALAPPDATA%\ms-playwright\`). If that download failed (offline, office network), install it manually:

```powershell
npx playwright install chromium
```

To drive a different browser instead, pass `--browser` with one of the official values (`chrome`, `firefox`, `webkit`, `msedge`), or `--executable-path <path>` for a custom binary — see the [browser options table in Chapter 0](./00_environment_setup.md#05-install-nodejs-and-the-playwright-mcp-server) and the [official configuration list](https://github.com/microsoft/playwright-mcp).

---

## 5.5 MCP server startup failures / long startup

**Symptom:** `[harness] MCP startup failed: servers did not start within 90 seconds`, or a server simply lists no tools.

**Causes & fixes:**

- **First-ever npx run** — it downloads the package; just run it again.
- **A leftover server process** from a previous crashed run keeps system resources busy. Clean up before running:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "playwright-mcp|duckduckgo-mcp-server|headless_shell|npx" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
```

- **The ddg server needs its venv** — make sure you run through the venv (`uv run --python ".venv" ...`) so `duckduckgo-mcp-server.exe` is found next to the Python executable.

`mcp_call()` reports every failure as an `"ERROR: ..."` string instead of raising, and `start_mcp()` records startup errors into the report — read those messages first; they usually name the exact cause.

---

## 5.6 Harmless noise: `MCPDeprecationWarning` and "exit code 1"

**Symptom:** walls of `MCPDeprecationWarning: The logging capability is deprecated...` in the log, and PowerShell reporting `Exit Code: 1` even though your script printed everything correctly.

**Cause:** (a) the MCP library writes deprecation warnings to stderr (the "error output" channel); (b) when PowerShell redirects stderr (`2>&1`), any stderr text is treated as an error and changes the exit code — even harmless warnings.

**Fix:** nothing to fix. Whenever you redirect stderr, judge success by the script's own output (`[done] report saved to ...`), not by the exit code. To see a clean exit code, send stderr to its own file or discard it.

---

## 5.7 Rate limits and `ref://` URLs (DuckDuckGo server)

- The ddg server prints its limits at startup: **30 searches/minute, 20 fetches/minute** (counted over the last minute), content cache keeps results for 300 s. Our pipeline uses only a few searches per run — never a problem. If you ever put it in a loop, add a short pause between searches.
- **Long URLs become `ref://...` texts** in search results (>120 chars) to save context tokens. If your results contain `ref://`, call the ddg server's **`expand_link`** tool to get the real URL back. (The store URLs in this project are short enough that this never happened.)

---

## 5.8 WAF-protected pages: empty content from plain fetchers

**Symptom:** `fetch_content` (or any HTTP fetcher) returns ~0 useful characters for `store.nintendo.com.hk`, or an HTTP **202** with an `x-amzn-waf-action: challenge` header.

**Cause:** AWS WAF JavaScript challenge — only a real browser that executes JS passes it. Plain HTTP clients cannot, *by design*.

**Fix:** use the Playwright browser path (`browser_navigate` + `browser_evaluate`) — which is what [Chapter 3's discovery](./03_mcp_client.md#35-the-discovery-that-shaped-the-project-fetch_content-is-not-enough) is all about. Also: allow up to ~20 seconds; the challenge needs a few seconds before the real page appears (our `fetch_webpage` asks 10 times, 2 s apart).

---

## 5.9 Model file problems

**Symptom:** crash on `Model(model_path)` — file not found.

**Checklist:**

- File is exactly at `model/gemma-4-E2B-it-qat-UD-Q2_K_XL.gguf` (name and folder must match; all scripts find paths relative to their own file, so you can run them from anywhere).
- The download finished completely (an interrupted download gives you a broken file; re-download if you are not sure).
- Enough RAM: the compressed file is small, but loading it plus a `n_ctx=100000` context is heavier than the file alone. If you are low on memory, use a smaller `n_ctx` like the pipeline does (`8192`).

---

## 5.10 "The SLM answers weirdly" — problems caused by the prompt

| Symptom | Likely cause | Fix |
|---|---|---|
| Model *describes* tools instead of calling them | JSON-schema sampler combined with tools — the JSON grammar does not allow the special tokens that tool calls need | Use the default sampler for tool chats ([Chapter 2.3](./02_basic_tool_calling.md#23-the-two-stage-pattern-and-a-critical-conflict)) |
| Model ignores the history you provided | the system prompt doesn't mention it | Include the exact phrase "Previous Conversation" in the system prompt (it must match the label `invoke()` adds) |
| The listing job outputs only one line | the system prompt was written for a different job ("You write short web search queries…") | Use the neutral prompt "Follow the instruction exactly and reply with the requested lines only." |
| Format tasks produce extra text | no example given | Add one example showing the exact output shape |
| Wrong arithmetic | small model | Never let the model compute; give it a tool (here: `add`) |
| Replies are cut short | the `max_completion_tokens` limit was reached | Raise the limit, or (better) make the task smaller |
| Reply "cuts off" mid-generation in the log | `stop_generation()` from the token limit | Expected behavior; the partial text stays in the history |

---

## 5.11 Token counting is off by one

`chat.tokenize(text)` **includes the BOS token** (the special "start of text" token), so the true token count is `len(tokenize(text)) - 1`. If you forget the `-1`, every limit is wrong by one — usually harmless, but remember it when you need exact numbers. Also note: the token *stream* gives you "pieces" that can contain several tokens at once (e.g. `"1\n"`), so exact counting must go through `tokenize()`.

---

## 5.12 Where things live (cheat sheet)

| Thing | Location | Safe to delete? |
|---|---|---|
| Files written by Playwright MCP | `%TEMP%\slm-playwright-mcp\` | Yes, anytime |
| Run reports | `output\run_<timestamp>.md` | Yes (keep the latest for reference) |
| Model file | `model\*.gguf` | Never (re-download only) |
| Virtualenv | `.venv\` | Yes, recreate with `uv venv` + `uv pip install -r requirements.txt` |
| Old `.playwright-mcp/` folder (from old runs) | project root | Yes — and it is in `.gitignore` |

Still stuck? Re-read [Chapter 4.6](./04_mcp_tool_calling.md#46-the-small-model-limitation-story) — most "impossible" problems in this project come from the small model's limits, and we found a fix in the harness code for every one of them.
