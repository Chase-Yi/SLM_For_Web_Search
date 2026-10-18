# Short setup checklist
(For the full step-by-step guide with explanations and checks, see docs/00_environment_setup.md)

# 1 - Create Virtual Environment with Python 3.14

# 2 - Install uv and necessary packages
## 2.1 - Command
```
pip install uv
uv pip install -r requirements.txt
```

# 3 - Install Node.js and the Playwright MCP server
The Playwright MCP server is a Node.js package (there is no Python SDK for it),
so it is NOT part of requirements.txt and must be installed with npm.

## 3.1 - Install Node.js (LTS) first: https://nodejs.org/

## 3.2 - Command
```
npm install -g @playwright/mcp
```

# 4 - Download Small Language Model File
## 4.1 - Download link (click to download):
[➜ gemma-4-E2B-it-qat-UD-Q2_K_XL.gguf](https://huggingface.co/unsloth/gemma-4-E2B-it-qat-GGUF/resolve/main/gemma-4-E2B-it-qat-UD-Q2_K_XL.gguf?download=true)

## 4.2 - Put this SLM File under the path of "model/gemma-4-E2B-it-qat-UD-Q2_K_XL.gguf"