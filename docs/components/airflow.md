# Airflow

## What it's for

A local Airflow stack that runs the [daily pipeline](daily-pipeline.md) as
one task per stage: the orchestration you would use at work, with retries
and backfills per stage. It needs a machine that is on at 06:00, so
[GitHub Actions](github-actions.md) is what actually fires the daily runs;
this is the stepping stone to running the stages as Kubernetes pods.

How to start it and its settings are in
[infra/airflow/README.md](../../infra/airflow/README.md), next to the code.

## Entry points

- The DAG `ankigen_daily`
  ([infra/airflow/dags/ankigen_daily.py](../../infra/airflow/dags/ankigen_daily.py)):
  daily at 06:00 in `America/Sao_Paulo`, ten tasks chained in `STAGES` order,
  each calling `pipeline.run_stage()` for the run's logical date.
- `docker compose --env-file ../../.env up -d --build` from `infra/airflow/`,
  and the UI at <http://localhost:8080>.

## Reads / writes

| Mount | In the container | |
|---|---|---|
| `ANKI_PROFILE_DIR` | `/opt/ankigen/anki`, read-only | Your Anki profile folder, the whole folder, so the `-wal` sidecar comes with it. |
| `pipelines/` | `/opt/ankigen/pipelines`, read-only | The profiles. |
| `data/` | `/opt/ankigen/data` | The warehouse, snapshots and `.apkg`, on the host. |

The stages pass nothing through XCom: each reads the
[warehouse](warehouse.md) and replaces its own `run_date` partition, so
retries and backfills are safe. `logical_date` is the run date, so a backfill
of 2026-09-01 reproduces that day's plan, not today's.

## Depends on

Airflow 3.3 (`apache/airflow:3.3.2`; the DAG also imports on 2.x) with the
package installed into the image, and Postgres 16 for Airflow's metadata.
`LLM_PROVIDER` (default `openrouter`) and `LLM_API_KEY` from the repo's
`.env`; [LLM providers](llm-providers.md).

## How to run and test it

```bash
cd infra/airflow
docker compose --env-file ../../.env up -d --build
docker compose exec airflow airflow dags test ankigen_daily 2026-09-01
docker compose exec airflow airflow dags backfill ankigen_daily -s 2026-09-01 -e 2026-09-07
```

There are no automated tests for the DAG; the stages it calls are tested
with the package ([Daily pipeline](daily-pipeline.md)).

## Limits and known issues

- `generate` has `retries=0` on purpose: it is the only stage whose output
  changes from run to run, so a retry spends quota to write *different*
  cards. Every other task retries twice.
- `max_active_runs=1`: DuckDB takes one writer at a time.
- No `ANKIGEN_PIPELINE` is set, so the DAG runs `data-platform`'s profile,
  with the warehouse in `data/` itself rather than under
  `data/pipelines/<id>/`.
- The image installs the package without extras or system packages: no
  `[push]`, so no AnkiWeb push; no `[gemini]` and no Claude CLI, so only the
  OpenAI-compatible providers work; no Graphviz, so diagrams fall back to a
  web search; and no `[guide]`, so the guide is HTML only.
- `LLM_API_KEY` is required even for a provider that needs none.
- One pod per stage on Kubernetes is planned in
  [#65](https://github.com/PedroLFerraz/ankigen/issues/65).
