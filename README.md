# Discord Python Runner

A tiny, resource-frugal Discord bot that **executes Python found in fenced code
blocks** and replies with the output. It is built to run on a single **Render
free-tier Web Service** (512 MB RAM, ~0.1 CPU) and to do as little work as
possible for ordinary chatter.

The bot **only** runs code inside a ```` ```python ```` or ```` ```py ```` block.
Everything else — normal conversation, questions like "can someone help with
Python?", other languages, unmarked code — is ignored. There is **no command
prefix** (no `!run`); the bot simply watches for Python code blocks.

> ⚠️ **Security, up front:** this bot executes code that people type. The
> isolation here (a locked-down subprocess, restricted builtins, an import
> blocklist, CPU/memory/time limits, and AST checks) is **defense-in-depth, not
> a real security boundary**. It is intended for a **trusted / private** server.
> Do **not** expose it to the public without a hardened sandbox (containers with
> seccomp/gVisor, nsjail, or a microVM). See [Security](#security).

---

## Contents

- [What it does](#what-it-does)
- [How it decides what to run](#how-it-decides-what-to-run)
- [Example messages](#example-messages)
- [Discord setup](#discord-setup)
- [Local setup](#local-setup)
- [Configuration](#configuration)
- [Deploy to Render](#deploy-to-render)
- [Free-tier behavior](#free-tier-behavior)
- [Security](#security)
- [Memory footprint & risks](#memory-footprint--risks)
- [Project structure](#project-structure)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## What it does

- Listens for messages containing a Python code block and runs the **first** one
  (configurable).
- Executes each snippet in a **separate, short-lived Python subprocess** with a
  stripped environment (so the bot token and other secrets are never visible to
  the code), a hard wall-clock timeout, and CPU / memory / output caps.
- Replies with a clean result:
  - `✅ Execution complete` + the output + `⏱️ 0.12s`
  - `❌ Execution failed` + the traceback
  - `⏱️ Execution timed out after 5 seconds.`
- Rate-limits per user and globally, and runs **one execution at a time** so the
  free instance is never overwhelmed.
- Serves a `GET /health` endpoint so Render's health checks pass. The health
  server does **no** code execution.

All state (rate-limit windows, the concurrency counter) is **in memory**. There
is no database and no Redis. A restart clears that state — which is fine and
expected.

---

## How it decides what to run

The listener is deliberately cheap. For every message it does only this:

```python
if message.author.bot:
    return
code = extract_python_code(message.content)   # pure string work, no I/O
if code is None:
    return
# ...only now does any real work (validation, subprocess, etc.) happen
```

The parser (`app/utils/code_parser.py`) accepts **only** fenced blocks whose
language tag is `python` or `py` (case-insensitive):

| Message | Runs? |
|---|---|
| ```` ```python\nprint("hi")\n``` ```` | ✅ yes |
| ```` ```py\nprint("hi")\n``` ```` | ✅ yes |
| ```` ```js\nconsole.log("hi")\n``` ```` | ❌ ignored (not Python) |
| ```` ```\nprint("hi")\n``` ```` (no language) | ❌ ignored by default* |
| `` `print("hi")` `` (inline) | ❌ ignored |
| `hello, can someone help with python?` | ❌ ignored |
| `!run print("hi")` | ❌ ignored (no command prefix exists) |

\* Set `REQUIRE_PYTHON_CODE_BLOCK=false` to also treat unmarked ```` ``` ````
blocks as Python.

If a message has several Python blocks, only the **first** runs by default (set
`EXECUTE_ALL_BLOCKS=true` to join and run them all — they are never concatenated
silently).

---

## Example messages

Post any of these in a channel the bot can see:

````text
```python
print("Hello from Discord!")
```
````

````text
```py
import math
print(math.factorial(10))
```
````

````text
```python
for i in range(5):
    print(i, i**2)
```
````

Timeouts and errors are reported cleanly:

````text
```python
while True:
    pass
```
````
→ `⏱️ Execution timed out after 5 seconds.`

````text
```python
print(1 / 0)
```
````
→ `❌ Execution failed` with a `ZeroDivisionError` traceback.

Blocked operations are refused **before** running:

````text
```python
import os
os.system("rm -rf /")
```
````
→ `🚫 Your code was blocked before running for safety reasons.`

---

## Discord setup

1. **Create the application.** Go to the
   [Discord Developer Portal](https://discord.com/developers/applications) →
   **New Application**.
2. **Add a bot.** Open the **Bot** tab → **Add Bot**.
3. **Enable the Message Content intent (required).** Still on the **Bot** tab,
   scroll to **Privileged Gateway Intents** and turn on **Message Content
   Intent**. Without this, `message.content` arrives empty and the bot can never
   see code blocks. (This bot does **not** need the Presence or Server Members
   intents — leave them off.)
4. **Copy the token.** Click **Reset Token** → copy it. This is your
   `DISCORD_TOKEN`. Treat it like a password; never commit it.
5. **Invite the bot.** Open **OAuth2 → URL Generator**:
   - Scopes: **`bot`**
   - Bot Permissions: **View Channels**, **Send Messages**, **Read Message
     History**, **Attach Files**

   Or use this URL (replace `YOUR_CLIENT_ID`; permission integer `101376` covers
   exactly those four):

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot&permissions=101376
   ```

   Open it, pick your server, and authorize.

That's the minimum. The bot needs no administrator rights and no member/presence
data.

---

## Local setup

Requires **Python 3.12**.

**1. Clone and enter the project**

```bash
git clone <your-repo-url> discord-python-runner
cd discord-python-runner
```

**2. Create and activate a virtual environment**

Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Windows (cmd):
```bat
python -m venv .venv
.venv\Scripts\activate.bat
```

macOS / Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**3. Install dependencies**

```bash
pip install -r requirements.txt
# for running the tests too:
pip install -r requirements-dev.txt
```

**4. Configure**

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Edit `.env` and set at least `DISCORD_TOKEN`.

**5. Run**

```bash
python -m app.main
```

You should see a log line like `Connected as <bot> ...`. Post a Python code
block in a channel the bot can see.

> **Note on Windows:** the strong isolation (address-space limits, process-group
> kill) relies on POSIX features and is only fully enforced on **Linux**. The
> bot still runs locally on Windows for development, but for real use deploy on
> Linux (Render or Docker). The wall-clock timeout works everywhere.

---

## Configuration

Everything is configured through environment variables (all optional except
`DISCORD_TOKEN`). Defaults are production-safe for the free tier.

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | — | **Required.** Bot token. Secret. |
| `DISCORD_WEBHOOK_URL` | _empty_ | Optional webhook for output delivery. Secret. |
| `OUTPUT_MODE` | `bot` | `bot`, `webhook`, or `both`. |
| `REQUIRE_PYTHON_CODE_BLOCK` | `true` | If `false`, unmarked ```` ``` ```` blocks are treated as Python. |
| `EXECUTE_ALL_BLOCKS` | `false` | If `true`, run every Python block in a message (joined). |
| `MAX_CODE_LENGTH` | `5000` | Max characters of code accepted. |
| `MAX_EXECUTION_TIME` | `5` | Wall-clock timeout (seconds) per run. |
| `MAX_OUTPUT_LENGTH` | `8000` | Max characters captured per stream. |
| `MAX_MEMORY_MB` | `256` | Address-space limit (MB) for each execution subprocess. |
| `MAX_CONCURRENT_EXECUTIONS` | `1` | Simultaneous executions. **Keep at 1** on the free tier. |
| `MAX_QUEUE_SIZE` | `2` | Extra requests allowed to wait before "busy". |
| `MAX_EXECUTIONS_PER_USER_PER_MINUTE` | `5` | Per-user rate limit. |
| `MAX_GLOBAL_EXECUTIONS_PER_MINUTE` | `15` | Global rate limit. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding-window length for rate limits. |
| `PORT` | `10000` | Health-server port. Render injects this automatically. |
| `HOST` | `0.0.0.0` | Health-server bind address. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

### Optional webhook output

Set `DISCORD_WEBHOOK_URL` and `OUTPUT_MODE=webhook` (or `both`) to post results
to a Discord webhook. The webhook URL is a **secret**: it is never logged and
never placed in the execution subprocess's environment. If a webhook-only
delivery fails or the URL is unset, the bot falls back to replying in-channel so
you are never left without a response. Short control notices (rate-limit,
"busy", validation errors) always reply in the originating channel.

---

## Deploy to Render

This repo includes `render.yaml` (a Blueprint) that provisions a single **free
Web Service** using Render's native Python runtime.

1. Push the repo to GitHub/GitLab.
2. In Render, choose **New → Blueprint** and point it at your repo. Render reads
   `render.yaml`.
3. When prompted, enter your secrets: `DISCORD_TOKEN` (and `DISCORD_WEBHOOK_URL`
   if you use one). These are marked `sync: false`, so they are never stored in
   the file.
4. Deploy. Render runs `pip install -r requirements.txt` then
   `python -m app.main`, and health-checks `GET /health`.

**Prefer Docker?** A lightweight, non-root `Dockerfile` is included. Either set
`runtime: docker` in `render.yaml` (and remove `buildCommand`/`startCommand`),
or build it anywhere:

```bash
docker build -t discord-python-runner .
docker run --rm -e DISCORD_TOKEN=xxx -e PORT=10000 -p 10000:10000 discord-python-runner
```

The health server binds `0.0.0.0:$PORT`, so Render (and Docker) can reach it.

---

## Free-tier behavior

Render's free Web Service **spins down after ~15 minutes with no inbound HTTP
requests**, and only wakes when a new HTTP request arrives. This matters for a
Discord bot: Discord talks to the bot over a **gateway WebSocket**, not over this
service's HTTP port, so gateway traffic does **not** count as activity and will
**not** keep the instance awake. Left alone, the service sleeps after ~15 minutes,
the process is stopped, and the bot goes offline until something hits its HTTP
endpoint again.

**To keep the bot online on the free tier, point an external uptime monitor at
`/health`** (e.g. [UptimeRobot](https://uptimerobot.com) or
[cron-job.org](https://cron-job.org)) on a ~10-minute interval. That steady
trickle of requests keeps the instance from spinning down. A single always-on
service fits within Render's free monthly instance-hours.

Other notes:

- **State resets on restart.** Rate-limit counters and the concurrency slot live
  in memory and reset to empty whenever the instance restarts. There is nothing
  to migrate or persist.
- **Cold starts.** If the instance *has* spun down, the first request afterward
  takes a few seconds while it wakes back up.
- **Always-on without a pinger** needs a service type that never sleeps — on
  Render that's a paid **Background Worker** (no HTTP port, never spun down). The
  included `Dockerfile` also runs unchanged on always-on hosts like Fly.io if you
  would rather not depend on a pinger.

---

## Security

**Threat model and honest limitations — please read.**

This bot runs code that users provide. It applies several layers of protection:

1. **Separate process.** Each snippet runs in its own `python -I -B` subprocess
   (`app/executor/runner.py`) that imports **none** of the bot's code and shares
   **none** of its memory.
2. **Secrets are absent, not hidden.** The subprocess gets a minimal, hand-built
   environment. `DISCORD_TOKEN`, the webhook URL, and every other host variable
   are simply **not present**, so executed code cannot read them.
3. **Restricted builtins + import guard.** Dangerous builtins (`eval`, `exec`,
   `open`, `compile`, `__import__`, `input`, …) are removed, and imports of a
   denylist of modules (`os`, `sys`, `subprocess`, `socket`, `ctypes`,
   `importlib`, `threading`, `multiprocessing`, networking libraries, …) are
   refused.
4. **AST pre-check.** Before a subprocess is even spawned, static analysis
   (`app/executor/validator.py`) rejects obvious attempts (blocked imports,
   blocked names, dunder-escape attributes) and reports syntax errors.
5. **Resource limits.** On Linux the subprocess gets an address-space cap
   (`RLIMIT_AS`), a CPU-time cap (`RLIMIT_CPU`), no core dumps, a file-size cap,
   and a process cap (anti fork-bomb). A wall-clock timeout kills the whole
   process group with `SIGKILL`, so even `while True: pass` cannot hang the bot.
6. **Bounded output.** Output is streamed and capped, so a program that prints
   forever cannot exhaust memory.

**Why this is still not a real sandbox.** Python was not designed to safely run
untrusted code in-process. Static analysis of Python can be defeated (there are
many creative ways to reach dangerous objects), and a restricted-builtins
namespace is not an escape-proof jail. The subprocess + OS limits are the
meaningful boundary here, and even those do not defend against every kernel-level
or side-channel attack.

**Why `exec()` in the bot process would be worse.** Running user code directly
in the bot process (e.g. `exec(user_code)`) would give it the bot's memory,
including the Discord token and any secrets, plus the ability to crash or hang
the whole service. This project deliberately does **not** do that; it always
uses an isolated subprocess with a stripped environment.

**Recommendation.** Use this on a **trusted / private** server among people you
know. For anything public or higher-risk, run executions inside a hardened
sandbox: a container with a seccomp profile and dropped capabilities, gVisor,
nsjail, or a per-execution microVM (e.g. Firecracker). The `CodeExecutor`
abstraction in `app/executor/executor.py` exists precisely so you can swap in
such a backend without touching the bot.

---

## Memory footprint & risks

Rough RAM budget on the 512 MB free instance:

| Component | Approx. RSS |
|---|---|
| Bot process (Python + discord.py + gateway, minimal intents, message cache disabled) | ~70–110 MB |
| Health server (FastAPI + uvicorn, single asyncio worker) | ~15–25 MB |
| One execution subprocess (typical snippet) | ~15–30 MB |
| Output buffers (≤ `MAX_OUTPUT_LENGTH` per stream) | ~16 KB |

So typical steady-state usage is roughly **100–150 MB**, leaving comfortable
headroom under 512 MB. Because only **one** execution runs at a time, subprocess
memory does not stack.

**Biggest risks and how they're mitigated:**

- **A single runaway allocation.** Mitigated by `RLIMIT_AS` = `MAX_MEMORY_MB`
  (default 256 MB); an over-allocating program gets a `MemoryError` or is killed.
  ⚠️ Keep `MAX_MEMORY_MB` well under 512 (≈ 256–350) so one execution can never
  approach the instance ceiling and trigger an OOM kill of the bot itself.
- **Unbounded output.** Mitigated by continuous draining capped per stream, so
  `print`-forever programs stay bounded.
- **Runaway CPU / infinite loops.** Mitigated by the wall-clock timeout
  (`SIGKILL` of the process group) with `RLIMIT_CPU` as a backstop.
- **Fork bombs / thread bombs.** Mitigated by `RLIMIT_NPROC` and by blocking the
  `threading`/`multiprocessing`/`subprocess` modules.
- **Bot-side memory growth.** Mitigated by minimal intents and by disabling
  discord.py's message cache (`max_messages=None`); rate-limit state is bounded
  and periodically swept.

If interpreter startup ever fails with a spurious `MemoryError` on your host
(some systems reserve a lot of *virtual* memory), raise `MAX_MEMORY_MB` (e.g. to
`384`).

---

## Project structure

```
discord-python-runner/
├── app/
│   ├── __init__.py
│   ├── main.py                 # entrypoint: bot + health server on one loop
│   ├── config.py               # env-var configuration (single source of truth)
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── discord_bot.py      # discord.Client + thin on_message listener
│   │   └── message_handler.py  # pipeline: validate→limit→execute→deliver
│   ├── executor/
│   │   ├── __init__.py
│   │   ├── executor.py         # CodeExecutor ABC + SubprocessExecutor
│   │   ├── runner.py           # standalone sandbox harness (no app imports)
│   │   ├── validator.py        # length + AST security checks
│   │   └── models.py           # ExecutionResult / ExecutionStatus
│   ├── services/
│   │   ├── __init__.py
│   │   ├── rate_limiter.py     # in-memory per-user + global limits
│   │   ├── concurrency.py      # one-at-a-time guard + tiny queue
│   │   └── webhook.py          # optional webhook delivery (httpx)
│   └── utils/
│       ├── __init__.py
│       ├── code_parser.py      # strict ```python``` detection
│       └── formatter.py        # Discord-ready result formatting
├── tests/
│   ├── conftest.py
│   ├── test_code_parser.py
│   ├── test_validator.py
│   ├── test_rate_limiter.py
│   ├── test_concurrency.py
│   └── test_executor.py
├── Dockerfile
├── .dockerignore
├── render.yaml
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

The suite covers the parser (valid `python`/`py` blocks, multiple blocks, JS
blocks, unmarked blocks, prose, empty, CRLF, inline code), the validator
(allowed code, blocked imports/builtins, dunder escapes, syntax errors, length),
the rate limiter (first-allowed, per-user and global limits, window expiry,
independence), the concurrency guard (single slot, queue, rejection), and the
executor (normal output, no output, runtime exceptions, timeout, output
truncation, blocked import/`open` at runtime, secret exclusion, and — on POSIX —
the memory limit).

Tests use `asyncio.run()` directly, so no async pytest plugin is required. The
executor tests spawn real subprocesses and are fully enforced on **Linux/macOS**;
the memory-limit test is skipped on non-POSIX platforms.

---

## Troubleshooting

**The bot is online but never responds to code blocks.**
Enable the **Message Content Intent** in the Developer Portal (Bot →
Privileged Gateway Intents). Without it, message text is empty. Also confirm the
block is tagged ```` ```python ```` or ```` ```py ````, and that the bot has
**View Channel** + **Send Messages** in that channel.

**`❌ An internal error occurred while running your code.`**
The execution subprocess failed to start or crashed unexpectedly. Check the
service logs. On non-Linux hosts, resource limits aren't applied; deploy on
Linux for full behavior.

**Everything returns a `MemoryError` immediately.**
Your host reserves a lot of virtual memory at interpreter startup. Raise
`MAX_MEMORY_MB` (e.g. `384`).

**`⚠️ You are executing code too quickly.` / `⚠️ Another execution is currently running.`**
Rate/concurrency limits are working as intended. Adjust
`MAX_EXECUTIONS_PER_USER_PER_MINUTE`, `MAX_GLOBAL_EXECUTIONS_PER_MINUTE`, or
`MAX_QUEUE_SIZE` if needed.

**Render shows the service as unhealthy.**
Confirm the service can bind `$PORT` (Render sets it automatically) and that
`GET /health` returns `{"status": "ok"}`. The health path is configured in
`render.yaml` as `/health`.

**Output is sent as a file attachment.**
Results longer than Discord's 2000-character message limit are delivered as a
`.txt` attachment automatically. Lower `MAX_OUTPUT_LENGTH` if you prefer shorter,
inline replies.

**A first request after idle is slow.**
That's a free-tier cold start while the instance wakes. Subsequent requests are
fast.

---

### License

Provided as-is, for use on trusted/private servers. Review the
[Security](#security) section before deploying.
