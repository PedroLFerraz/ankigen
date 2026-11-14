# LLM providers and embeddings

## What it's for

Every model call goes through one module, `src/ankigen/llm.py`, and every
provider setting is resolved in one place, `src/ankigen/config.py`. Between
them they decide who writes the cards (the **writer**), who checks them (the
**checker**), where requests go when the writer cannot answer (the
**fallback**), and which model embeds text for dedup. Every call reports its
model and token use, which the run report adds up.

## Entry points

- `llm.call_json(prompt, cfg=...)`: one JSON answer, walking down the
  configured model chain. Used by generate, verify, refill, guide, `book`,
  `parts` and `add-theme`.
- `llm.check_image(image, card, query)`: a vision check of a searched picture
  ([Images](images.md)).
- `llm.preflight()`: checks the providers could work at all, with no network
  call, before a run spends ninety seconds on ingest.
- `settings.resolve_llm()`, `resolve_verify()`, `resolve_embedding()`: the
  effective provider, model chain, key and threshold.
- `dedup.Embedder` (`src/ankigen/dedup.py`): embeddings with a cache in the
  warehouse.
- `ankigen providers`: lists the presets, prints the resolved settings and
  tests the connection.

## Reads / writes

Settings come from the environment and `.env`; a pipeline's `pipeline.yaml`
`models:` sets the same variables for a run of it
([Profiles and pipelines](profiles-and-pipelines.md)).
[.env.example](../../.env.example) documents every one.

| Role | Settings | Providers |
|---|---|---|
| writer | `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`; `CLAUDE_MODEL` for Claude | `ollama`, `groq`, `nvidia`, `openrouter`, `sambanova`, `mistral` (any OpenAI-compatible endpoint), `claude` (the Claude Code CLI) |
| checker | `VERIFY_PROVIDER`, `VERIFY_MODEL` | the same; empty means the writer checks its own cards |
| fallback | `FALLBACK_PROVIDER`, `FALLBACK_VERIFY_MODEL` | where every request goes once the writer cannot answer at all |
| embeddings | `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL_NAME`, `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL`, `SEMANTIC_THRESHOLD` | `fastembed` (in-process), `ollama`, `nvidia`, `openrouter`, `mistral` |
| guards | `MAX_LLM_CALLS`, `ALLOW_PAID_MODELS` | a per-run call cap; and a refusal to call any OpenRouter model without `:free` |

Model settings are **chains**, best first (`LLM_MODEL=a,b,c`). Free tiers
meter each model separately, so the run walks down the chain as models run
out or stay busy. A busy model is tried
once and skipped for ten minutes: on the free tier even a 503 seems to count
against the day.

Three setups are worth knowing:

| | Generation | Free limits (measured Sept 2026) |
|---|---|---|
| **Most headroom** | `groq` | 1000 requests/day |
| **Best cards** | `claude` | the subscription's usage limit |
| **Offline** | `ollama` | unlimited, and noticeably weaker cards |

A run costs about seven generation calls and seven checking calls, plus one
per searched picture and two per chapter of the study guide. That is why
`VERIFY_MODEL` pointing at a second model matters on a metered free tier: it
doubles the allowance *and* gives an independent opinion.

```
LLM_PROVIDER=claude
VERIFY_MODEL=claude-sonnet-5   # a different model checks what the first wrote
```

**Claude** runs through the Claude Code CLI on a Claude subscription (Pro or
Max): a local `claude` login, or `CLAUDE_CODE_OAUTH_TOKEN` from `claude
setup-token`. The CLI is called with no tools, no settings and a one-line
system prompt, and every Anthropic API key is removed from its environment,
so a call can only ever run on the subscription. It also looks at the web
pictures `check_image` is asked about: the picture is written to a temporary
directory that is the CLI's working directory, and `Read` is the one tool it
is given. Once Claude says its usage limit is reached (or there is no login,
or no CLI), the run fails before anything is pushed, unless `FALLBACK_PROVIDER`
names an OpenAI-compatible provider to take over. A short or unchecked day
never counts as a finished one.

**Embeddings** are configured separately, because most free chat APIs serve
none. Left unset they come from the chat provider when it has them, and
otherwise from **fastembed** (`BAAI/bge-small-en-v1.5`), which runs an ONNX
model in-process: no server, no key, no quota, and the only option that works
unchanged on a CI runner. The duplicate threshold belongs to the embedding
model, since each one's similarity scale differs. Vectors are cached in the
warehouse's `embedding_cache` by content hash and model, so after the first
run only new or edited notes are embedded.

## Depends on

The `openai` SDK for every OpenAI-compatible provider; the `claude` binary on `PATH` for Claude;
`fastembed` and NumPy for embeddings. Writes nothing itself except the
embedding cache, through the [warehouse](warehouse.md).

## How to run and test it

```bash
ankigen providers        # resolved settings, and a connection test
```

The tests never call a real provider: `tests/conftest.py` replaces
`llm.call_json` and the embedder with deterministic fakes.

```bash
python -m pytest tests/test_config.py tests/test_claude.py tests/test_generate_verify.py
```

`tests/test_claude.py` checks the CLI call (and that API keys are stripped)
with a stub `claude`; `test_generate_verify.py` covers JSON parsing, retries
and fallbacks.

## Limits and known issues

- Free-tier limits and model ids change often; the presets in `PROVIDERS`
  are starting points. `ankigen providers` shows a retired model as a failed
  connection.
- The English-only fastembed model embeds Portuguese cards too:
  [#75](https://github.com/PedroLFerraz/ankigen/issues/75).
- Amazon Bedrock as a provider:
  [#63](https://github.com/PedroLFerraz/ankigen/issues/63). A cost per run:
  [#62](https://github.com/PedroLFerraz/ankigen/issues/62).
