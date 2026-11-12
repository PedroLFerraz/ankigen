---
name: llm-prompts
description: Use when editing a prompt template, adding a model call, or changing how providers are chosen. Covers prompts/*.txt, llm.call_json and the writer/checker/fallback settings.
---

# LLM prompts and providers

- Prompts are `string.Template` files in `src/ankigen/prompts/`, shipped as
  package data and read with `importlib.resources` (see `breakdown.py` or
  `guide.py` for the small loader). Substitute with `Template(text).substitute(...)`
  so a missing placeholder fails loudly. The list of templates and who uses
  each is in [docs/components/card-types-and-prompts.md](../../../docs/components/card-types-and-prompts.md).
- Every model call goes through `llm.call_json(prompt, cfg=...)`, which returns an
  `LLMResult` and records tokens for the run report. Do not call a provider SDK
  from a stage.
- Providers (`llm.py`, `config.py`): the **writer** (`LLM_PROVIDER`, `LLM_MODEL`),
  the **checker** (`VERIFY_PROVIDER`, `VERIFY_MODEL`) and the **fallback**
  (`FALLBACK_PROVIDER`). Model settings are chains, best first. Presets and
  settings are in [docs/components/llm-providers.md](../../../docs/components/llm-providers.md)
  and `.env.example`. `ankigen providers` shows what is resolved.
- Guards: `MAX_LLM_CALLS` caps calls per run, and `ALLOW_PAID_MODELS` must be on
  to call an OpenRouter model without `:free`.
- Changing a prompt changes what tests' `FakeLLM` matches on: it recognises a
  prompt by a phrase in it (for example `fact-checker`). If you reword that
  phrase, update `tests/conftest.py` too.
