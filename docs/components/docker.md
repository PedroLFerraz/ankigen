# Docker

## What it's for

One image that has everything a run uses, so `docker run ... ankigen run`
needs nothing installed on the host: the package with every extra, Graphviz
and Pango, and the Claude Code CLI. It is about 1.2 GB on disk, 453 MB to
pull.

## Entry points

- [Dockerfile](../../Dockerfile) at the repo root. Its entry point is
  `ankigen`, so the arguments after the image are a [CLI](cli.md) command;
  with none it prints `--help`.
- The published image, `ghcr.io/pedrolferraz/ankigen`, tagged with the commit
  SHA and `latest`. [docker.yml](../../.github/workflows/docker.yml) builds
  and scans it on every pull request that could change it, and pushes it on
  `master`.

```bash
IMAGE=ghcr.io/pedrolferraz/ankigen
docker pull "$IMAGE"                                # or build it: docker build -t "$IMAGE" .
ANKI=~/.local/share/Anki2/"User 1"                  # the folder holding collection.anki2
docker run --rm -v "$ANKI":/anki:ro -v ./data:/data "$IMAGE" plan     # no key needed
docker run --rm -v "$ANKI":/anki:ro -v ./data:/data --env-file .env \
  -e EMBEDDING_PROVIDER=fastembed "$IMAGE" run
```

## Reads / writes

| Path | | |
|---|---|---|
| `/anki` | read-only mount | your Anki profile folder; `ANKI_COLLECTION_PATH=/anki/collection.anki2` |
| `/data` | volume | everything the run writes (`DATA_DIR=/data`); the package and guide land in `./data/out/<date>/` |
| `/app/pipelines` | in the image | the repo's pipelines; pick one with `-e ANKIGEN_PIPELINE=historia-brasil` |

Keys come from `--env-file` at run time and are never in the image. To bring
your own profile: `-v ./profile.yaml:/profile.yaml:ro -e
ANKIGEN_PROFILE=/profile.yaml`. With no local Anki, do what CI does: skip the
`/anki` mount, run `ankigen pull` first, and pass
`-e ANKI_COLLECTION_PATH=/data/anki/collection.anki2`.

## Depends on

Three build stages, all on Debian bookworm so the copied binary finds the
glibc it was built against:

1. `node:22-bookworm-slim`: `npm install -g @anthropic-ai/claude-code`. Only
   the native `claude` binary is copied out; the runtime has no Node.
2. `python:3.12-slim-bookworm`: the package with `[gemini,push,guide]` into a
   venv, which the runtime copies whole, with no pip or build tools.
3. `python:3.12-slim-bookworm` runtime: Graphviz, Pango and CA certificates,
   the venv, the `claude` binary and `pipelines/`, running as user
   `ankigen`, uid 1000.

`.dockerignore` keeps the build context to `pyproject.toml`, `src/` and
`pipelines/`; `data/` alone is 100 MB of snapshots.

## How to run and test it

```bash
docker build -t ankigen .
docker run --rm ankigen --help
docker run --rm -v "$ANKI":/anki:ro -v ./data:/data ankigen plan
```

In CI, `docker.yml` builds the image and scans it with Trivy, failing on a
CRITICAL vulnerability that has a fix. The scan's table is in the run's
summary.

## Limits and known issues

- The container runs as uid 1000, because Anki makes the collection readable
  by its owner only. If yours is another uid, add
  `--user "$(id -u):$(id -g)" -e HOME=/tmp`.
- There is no Ollama in the container, so `-e EMBEDDING_PROVIDER=fastembed`
  overrides an `.env` written for one.
- `trivy-action` is pinned to a commit, not a tag: its tags were once moved
  to malicious commits (March 2026).
- Running the image on a schedule in AWS and Kubernetes:
  [#61](https://github.com/PedroLFerraz/ankigen/issues/61),
  [#64](https://github.com/PedroLFerraz/ankigen/issues/64).
