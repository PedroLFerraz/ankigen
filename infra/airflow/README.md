# Airflow

Runs the ten pipeline stages daily at 06:00 as ten tasks. What the stack
leaves out, and how it fits with the rest, is on the
[Airflow](../../docs/components/airflow.md) page.

```bash
docker compose --env-file ../../.env up -d --build
```

`--env-file` is not optional: Compose looks for `.env` next to the compose
file, and the settings live in the repo root one instead. The stack refuses to
start without `LLM_API_KEY` and `ANKI_PROFILE_DIR`.

The UI is at <http://localhost:8080>, with the login turned off for local use.

| | |
|---|---|
| `ANKI_PROFILE_DIR` | Your Anki profile folder, mounted read-only. The whole folder, not just the file, so the `-wal` sidecar comes with it. |
| `data/` | Mounted read-write: the warehouse, the snapshots and the `.apkg` land on the host, not in the container. |

## Notes

The stages pass nothing through XCom. Each reads the warehouse and replaces its
own `run_date` partition, so retries and backfills are safe:

```bash
docker compose exec airflow airflow dags backfill ankigen_daily -s 2026-09-01 -e 2026-09-07
```

`generate` is deliberately the one task with `retries=0`. It is the only stage
that calls the model, so a retry spends quota generating *different* cards
rather than repeating work.

Two containers, not the six in Airflow's own compose file: `airflow standalone`
runs the scheduler, API server and DAG processor in one, which is enough for a
personal daily DAG.
