# The whole pipeline in one image: `docker run ... ankigen run` needs nothing
# installed on the host. The collection is mounted read-only at /anki and
# everything the run writes goes to the /data volume. Keys come from the
# environment at run time (--env-file .env); none is baked into a layer.
#
# Every stage is on bookworm, so the binary copied out of the first one finds
# the glibc it was built against in the last.

# Claude writes and checks the cards, through the Claude Code CLI on the
# subscription behind CLAUDE_CODE_OAUTH_TOKEN (see llm.py). npm is only how
# it is fetched: the package is one native binary that needs nothing but
# glibc, so the runtime takes that file and no Node.
FROM node:22-bookworm-slim AS claude
RUN npm install -g @anthropic-ai/claude-code

# The package and its extras into a venv, which the runtime copies whole:
# no pip, no wheels and no build tools left behind.
# [push] for AnkiWeb, [guide] for the PDF.
FROM python:3.12-slim-bookworm AS build
RUN python -m venv /opt/venv
COPY pyproject.toml /src/
COPY src /src/src
RUN /opt/venv/bin/pip install --no-cache-dir "/src[push,guide]"

FROM python:3.12-slim-bookworm

# Graphviz draws the diagrams the model describes for a card (visuals.py);
# without it those cards fall back to searching the web for a picture.
# Pango is what WeasyPrint lays the study guide's PDF out with.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        graphviz libpango-1.0-0 libpangoft2-1.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=claude /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe \
    /usr/local/bin/claude

COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# The repo's pipelines, so `run` works without mounting a profile. Installed,
# the package finds them relative to the working directory (config.py).
# Mount your own over /app/pipelines, or one profile with ANKIGEN_PROFILE.
WORKDIR /app
COPY pipelines /app/pipelines

# Anki writes the collection readable by its owner only, so the user reading
# the mount has to be that owner: uid 1000, the first user on a Linux desktop.
# The Claude CLI keeps its state in $HOME, so the user needs a real one.
RUN useradd --uid 1000 --create-home ankigen
USER ankigen

# Embeddings in-process: a container has no Ollama on localhost, and this is
# what the daily workflow uses too.
ENV DATA_DIR=/data \
    ANKI_COLLECTION_PATH=/anki/collection.anki2 \
    EMBEDDING_PROVIDER=fastembed
VOLUME /data

# Links the package on ghcr.io to this repository.
LABEL org.opencontainers.image.source=https://github.com/PedroLFerraz/ankigen

ENTRYPOINT ["ankigen"]
CMD ["--help"]
