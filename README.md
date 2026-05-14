# Sedale GPT — A2A Capital Formation Agent

A public, A2A-protocol-callable agent that designs dynamic capital stacks for infrastructure-scale projects ($50M–$5B+). Trained on Sedale Turbovsky's approach; not the real Sedale. Built with [`a2a-sdk`](https://pypi.org/project/a2a-sdk/) + Anthropic Claude Opus 4.7 + [Exa](https://exa.ai). Deploys to Fly.io.

> **Disclaimer.** Output is research-and-strategy, not investment advice. Capital stacks involving securities require qualified securities counsel.

## What it does

For each project brief, returns two artifacts:

1. **`capital_stack_memo`** — markdown memo (≤2,000 words), every claim cited.
2. **`capital_stack_structured`** — JSON object with tranches, sequencing, named opportunities, and risks. Schema version 1.0.0 (see [system_prompt.md](src/sedale_gpt/system_prompt.md)).

Domains in scope: utility-scale energy + storage, transmission, water, broadband (incl. BEAD), ports, semis, advanced manufacturing, green hydrogen, carbon management, critical minerals.

## Endpoints

| Route | Auth | Tools |
|---|---|---|
| `GET /.well-known/agent-card.json` | — | public AgentCard |
| `GET /.well-known/agent-card/privileged` | — | privileged AgentCard |
| `POST /a2a/...` (JSON-RPC) | open | `exa_search`, `exa_get_contents`, `web_fetch` |
| `POST /a2a/privileged/...` (JSON-RPC) | `Authorization: Bearer $SEDALE_GPT_PRIVILEGED_TOKEN` | open tools + `draft_email`, `create_attio_note`, `create_attio_deal` (via Composio into Sedale's accounts) |
| `GET /healthz` | — | liveness |

## Local dev

```sh
cd agents/sedale-gpt
python -m venv .venv && . .venv/bin/activate         # or `.venv\Scripts\activate` on Windows
pip install -e ".[dev]"
cp .env.example .env                                  # fill in keys
export $(cat .env | xargs)                            # PowerShell users: load by hand
python -m sedale_gpt
```

Then in another shell:

```sh
curl http://127.0.0.1:8080/.well-known/agent-card.json | jq
curl http://127.0.0.1:8080/healthz
```

## Tests

```sh
pip install -e ".[dev]"
pytest
```

Tests are hermetic — no network, no Anthropic, no Composio.

## Deploy via Fly Launch (web UI)

1. Open <https://fly.io/launch> in your browser (sign in if needed).
2. Paste the repo URL: `https://github.com/sedaleturbovsky/sedale-gpt`
3. Fly scans the repo, detects `fly.toml` + `Dockerfile`, and pre-fills:
   - App name: `sedale-gpt`
   - Region: `iad`
   - Internal port: `8080`
4. When prompted, set these secrets in the UI:
   - `ANTHROPIC_API_KEY`
   - `EXA_API_KEY`
   - `PUBLIC_URL` = `https://sedale-gpt.fly.dev`
   - *(optional, only for the privileged route)* `COMPOSIO_API_KEY`, `COMPOSIO_USER_ID`, `SEDALE_GPT_PRIVILEGED_TOKEN`
5. Click **Deploy**. Fly builds the image with its remote builder and rolls out one Machine in `iad`.

Single shared-CPU machine in `iad` with `auto_stop_machines = "suspend"`. Healthcheck hits the cheap AgentCard route (no LLM call).

### Shipping code changes after the first deploy

Fly's web UI does not auto-pull new commits. To ship a code change, pick one:

- **Re-run Fly Launch** — open <https://fly.io/launch> with the same repo URL. Fly recognizes the existing app and ships a new release.
- **CLI fallback** — `fly auth login` (once), then from `agents/sedale-gpt/` run `fly deploy`.
- **No-rebuild redeploy** — Fly dashboard → app → Releases → "Redeploy" replays the existing image (useful for a restart, not for code changes).

If you later want auto-deploy on push, restore the deploy workflow:

```sh
git revert <commit-that-removed-fly-deploy.yml>
gh secret set FLY_API_TOKEN -R sedaleturbovsky/sedale-gpt   # paste a `fly tokens create deploy` value
git push
```

## Calling it from another agent

```python
from a2a.client import A2AClient
from a2a.types import Message, TextPart

client = A2AClient("https://sedale-gpt.fly.dev/a2a")

async for event in client.send_message_stream(
    Message(parts=[TextPart(text=(
        "Build a capital stack for a 200MW solar + 100MWh storage project "
        "in New Mexico. CapEx ~$280M. Offtaker: investment-grade IOU "
        "with a 20-year PPA. Sponsor has 30% equity to commit."
    ))])
):
    print(event)
```

The stream emits `TaskStatusUpdateEvent`s during research and two `TaskArtifactUpdateEvent`s at the end (memo, then JSON), then `TASK_STATE_COMPLETED`.

## Guardrails

- **Open endpoint, contained blast radius.** The public route has no auth but cannot touch Sedale's Gmail or Attio — those are gated behind the privileged route.
- **Cost ceilings.** `DAILY_INPUT_TOKEN_CEILING` / `DAILY_OUTPUT_TOKEN_CEILING` per UTC day; once tripped, new tasks fail fast. Concurrency capped at `MAX_CONCURRENT_TASKS` (default 3). v1 counters are process-local.
- **Tool-loop cap.** ≤12 iterations per task; on overflow, the task returns `TASK_STATE_INPUT_REQUIRED` rather than burning more tokens.
- **Cancellation.** `cancel()` flips an `asyncio.Event`; the loop checks it between iterations.
- **Hard disclaimers.** Every memo and JSON output carries the "not investment advice / counsel required" line.

## v1 limitations

- Process-local task store (`InMemoryTaskStore`) — single Fly machine. For multi-instance, swap in a shared store (Redis / Postgres).
- No per-caller auth on the open route. If you discover abuse, switch the AgentCard to declare a bearer auth scheme and move the token to a route-level middleware.
- No A2A push notifications. Caller polls or holds the SSE stream open.
- No eval harness yet. Port from [the in-house Strategist eval](../01-strategist.md) when ready.

## Files of interest

- [`src/sedale_gpt/system_prompt.md`](src/sedale_gpt/system_prompt.md) — versioned persona + output contract.
- [`src/sedale_gpt/agent_executor.py`](src/sedale_gpt/agent_executor.py) — the Anthropic tool-use loop wrapped in A2A events.
- [`src/sedale_gpt/agent_card.py`](src/sedale_gpt/agent_card.py) — both AgentCards.
- [`src/sedale_gpt/tools/registry.py`](src/sedale_gpt/tools/registry.py) — open-vs-privileged tool dispatcher.
- [`fly.toml`](fly.toml) + [`Dockerfile`](Dockerfile) — deployment.

## Note on the a2a-sdk import surface

The `a2a-sdk` API has moved fast across 0.x→1.x releases. The imports in this code target `a2a-sdk>=1.0.3` (May 2026) as documented in the helloworld example at [a2a-samples](https://github.com/a2aproject/a2a-samples). If your installed SDK differs, the most likely fixes are in:

- `agent_card.py` — `AgentInterface(protocol_binding=...)` may be named `AgentInterface(transport=...)` on some pre-1.0 versions.
- `agent_executor.py` — `from a2a.types.a2a_pb2 import ...` lives at that path in current main; older versions had these as plain pydantic classes under `a2a.types`.
- `app.py` — `create_jsonrpc_routes(handler, prefix)` is the current factory; some older docs showed `A2AStarletteApplication(...)` instead.

Run `pytest tests/test_agent_card.py` first when bumping the SDK — that's the canary.
