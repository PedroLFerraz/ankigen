---
name: testing
description: Use when writing or running tests for ankigen. Covers the offline fixtures, how CI splits the suite, and the system packages some tests need.
---

# Testing

```bash
pip install ".[dev,push,gemini,guide]"
python -m ankigen pipelines validate
python -m pytest -q                      # or: python -m pytest -q tests/test_dedup_export.py
```

- Tests never touch the network or a real provider. `tests/conftest.py` pins the
  provider and gives you: `fake_llm` (a `FakeLLM` that answers generate, verify
  and guide prompts with deterministic output; `bad_words`, `distinct`,
  `fail_verify` steer it), `fake_embeddings`, `wh` (a temp warehouse), `cfg`,
  `profile`, and real SQLite collections in both Anki schemas
  (`modern_collection`, `legacy_collection`, `make_collection`).
- One test file per component (`tests/test_<module>.py`); `test_pipeline.py` and
  `test_e2e.py` run the whole pipeline.
- `.github/workflows/tests.yml` runs one job per component with only the extras
  it needs, then e2e. A new test file must be added to a job's `tests:` list.
  Do not edit the workflow unless the task is about it.
- Image tests need `graphviz`; guide tests need `libpango-1.0-0
  libpangoft2-1.0-0` and the `guide` extra. Without them those tests fail, so
  say so in the PR instead of skipping them.
- Never delete, skip or weaken a test to make it pass.
