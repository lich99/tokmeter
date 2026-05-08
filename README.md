# tokmeter

Local dashboard for what Claude Code and Codex CLI are actually costing you.

Reads `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**/*.jsonl`
directly off your disk, aggregates with polars, serves a single-file HTML on
`localhost:8765`. No auth, no upload, no telemetry.

<p align="center">
  <img src="assets/image.png" alt="tokmeter dashboard" width="900" />
</p>

```bash
uvx tokmeter
```

That's it. Opens the browser. `Ctrl-C` to exit.

---

## Install

| | command |
|---|---|
| **uv (recommended)** | `uvx tokmeter` |
| **uv, persistent**   | `uv tool install tokmeter` |
| **single-file mode** | `curl -O https://raw.githubusercontent.com/lich99/tokmeter/main/tokmeter.py && uv run tokmeter.py` |
| pipx                 | `pipx install tokmeter` |
| pip                  | `pip install tokmeter` |

`uv run tokmeter.py` works without a venv — the script declares its
dependency inline via [PEP 723](https://peps.python.org/pep-0723/).

```bash
tokmeter --port 8866      # change port
tokmeter --host 0.0.0.0   # bind to LAN
tokmeter --no-open        # don't auto-open browser
```

`TOKMETER_PORT` env var is equivalent to `--port`.

---

## What it shows

- **Both Claude Code and Codex CLI** in one chart, switchable per source.
- **Real-time**: a 30 s background rescan picks up new sessions
  (mtime + size delta only — minimal I/O); the browser polls every 60 s.
- **Arbitrary range**: From / To picker plus presets `1H / 5H / 24H / 7D / 30D / All`.
- **Adaptive bucketing**: `Auto` picks `1m / 5m / 15m / 30m / 1h / 6h / 1d`
  based on window width; can be forced.
- **Three timezones**: `America/New_York (ET)` / Local / UTC.
- **Cost-over-time** stack: by role (main vs subagent) or by token tier.
- **Cost split** donut: input / output / cache-write 5m / cache-write 1h /
  cache-read for Claude; cached / output text / reasoning / input for Codex.
- **By project / by model** breakdowns.
- **Buckets table**: 100k+ rows scrolled virtually, jump-to-time supported.
- **Gaps toggle**: `Fill` (zero-pads empty buckets) / `Skip` (sparse only).
- **Path de-mangling**: `-Users-nek0-Code-Claw-proxy` → `~/Code/Claw_proxy`
  by walking the filesystem to disambiguate the `_` → `-` collision.

---

## Pricing rules

Anthropic published list prices (USD / MTok):

| family | input | output | cache w 5m | cache w 1h | cache read |
|---|---:|---:|---:|---:|---:|
| Opus 4.5 / 4.6 / 4.7 | 5.00 | 25.00 | 6.25 | 10.00 | 0.50 |
| Opus 4 / 4.1 | 15.00 | 75.00 | 18.75 | 30.00 | 1.50 |
| Sonnet 4 / 4.5 / 4.6 | 3.00 | 15.00 | 3.75 | 6.00 | 0.30 |
| Haiku 4.5 | 1.00 | 5.00 | 1.25 | 2.00 | 0.10 |
| Haiku 3.5 | 0.80 | 4.00 | 1.00 | 1.60 | 0.08 |

Token figures come from
`usage.cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`
— the real billing buckets that `ccusage` and friends drop, leading them to
under-count by 10–30 % on heavy cache-write workloads.

OpenAI published list prices for Codex's GPT-5.x family are bundled too,
including the **>272 K long-context tier** (2× input/cached, 1.5× output)
and the **Priority Processing** multipliers when Codex Fast is on. The
service tier for each call is read from `~/.codex/logs_2.sqlite` because
`config.toml` only sets the default and many users toggle it mid-thread.

> If you're on the Anthropic Max plan or an OpenAI ChatGPT plan, your real
> bill is the flat subscription. The dollars here are the **equivalent
> standard-API cost** of the same workload — useful for comparing intensity
> across days, projects, or the two CLIs. Not your actual invoice.

---

## API

```
GET /                                                  dashboard HTML
GET /api/refresh                                       force rescan, returns {scanned,changed,removed}
GET /api/aggregate?from=<ms>&to=<ms>&granularity=<g>&tz=<tz>&gaps=<gaps>&source=<src>
                                                       aggregated buckets for the window
```

- `granularity`: `1m | 5m | 15m | 30m | 1h | 6h | 1d`
- `tz`: `ET | UTC | LOCAL` (LOCAL is normalized client-side; server treats it as UTC)
- `gaps`: `fill` (zero-pad empty buckets, default) | `skip` (omit empties)
- `source`: `cc` | `codex` | `all`

```bash
curl http://127.0.0.1:8765/api/refresh
curl "http://127.0.0.1:8765/api/aggregate?from=$(($(date +%s)*1000-18000000))&to=$(($(date +%s)*1000))&granularity=15m&tz=ET&source=all"
```

---

## Files

```
tokmeter/
├── tokmeter.py        HTTP server + scanner + polars aggregation
├── dashboard.html     single-file frontend
├── pyproject.toml
├── LICENSE            (MIT)
└── README.md
```

`dashboard.html` is re-read from disk on every `GET /` — **edit CSS, refresh
the browser, no restart needed** during development.

---

## Known limits

- **`<synthetic>` rows are skipped.** Claude Code emits these as
  zero-token placeholders when you interrupt mid-stream or a hook short-
  circuits. Filtered at parse time, not counted.
- **Agent SDK / OpenClaw / NanoClaw with `CLAUDE_CODE_OAUTH_TOKEN`** don't
  write to `~/.claude/projects/` — invisible to tokmeter, visible only in
  the Anthropic Console.
- **`claude -p` with `ANTHROPIC_API_KEY` set or `--bare`** logs locally but
  bills through the API account, not your Max plan.
- 1 M context windows on Opus 4.7 / 4.6 / Sonnet 4.6 are billed at standard
  rates (no long-context surcharge); Codex GPT-5.x crossing 272 K tokens
  triggers the long-context tier and is priced accordingly.

---

## License

MIT. See [LICENSE](./LICENSE).
