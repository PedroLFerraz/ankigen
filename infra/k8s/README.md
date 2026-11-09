# Kubernetes

Runs `ankigen run` daily as a `CronJob` on a local [kind](https://kind.sigs.k8s.io)
cluster. It uses the repo's [Dockerfile](../../Dockerfile) image, so it needs
docker, `kind` and `kubectl` on the host and nothing else.

```bash
cp ../../.env.example ../../.env      # fill in the keys; see below
make kind-up                          # cluster + image built and loaded into it
make deploy                           # namespace, Secret, ConfigMap, PVC, CronJob
make run                              # a manual Job from the CronJob, then its logs
# the same by hand:
kubectl -n ankigen create job --from=cronjob/ankigen manual-1
kubectl -n ankigen logs -f job/manual-1 --all-containers --prefix
make kind-down
```

| Object | What it is |
|---|---|
| `Secret/ankigen-env` | Made by `make deploy` from the repo's `.env` and mounted as environment variables. Never committed (`.env` is ignored). |
| `ConfigMap/ankigen-profile` | The pipeline's `profile.yaml` (`PIPELINE=historia-brasil make deploy` for the other one), read through `ANKIGEN_PROFILE`. |
| `PVC/ankigen-data` | `DATA_DIR=/data`: the warehouse, the collection's working copy and `out/<date>/ankigen_<date>.apkg`. |
| `CronJob/ankigen` | 06:00 UTC, `concurrencyPolicy: Forbid`, no retries, runs as uid 1000 without privileges. |

The pod has no Anki on the machine, so an init container runs `ankigen pull`
first, as CI does: the `.env` needs `ANKIWEB_USERNAME` and `ANKIWEB_KEY` (see
[AnkiWeb sync](../../docs/components/ankiweb-sync.md)), plus the LLM settings
(`CLAUDE_CODE_OAUTH_TOKEN` or `LLM_API_KEY`). `EMBEDDING_PROVIDER=fastembed`
comes from the image. The job does not push; the day's `.apkg` is on the
volume under `/data/out/<date>/`.

`kubectl logs` shows each stage, since the pipeline logs to stdout.
