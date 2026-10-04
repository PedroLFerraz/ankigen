import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root when running from a checkout; the working directory otherwise
# (e.g. inside a container, where the package is installed into site-packages).
_REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = _REPO_ROOT if (_REPO_ROOT / "pyproject.toml").exists() else Path.cwd()


def _default_collection() -> str:
    """Anki desktop's collection, when exactly one profile exists."""
    root = Path(os.environ.get("APPDATA", Path.home() / ".local/share")) / "Anki2"
    found = sorted(root.glob("*/collection.anki2")) if root.exists() else []
    return str(found[0]) if len(found) == 1 else ""


# Providers that speak the OpenAI chat-completions protocol. Ollama was always
# reached this way, so pointing at a hosted endpoint is a base URL plus a key.
#
# Free-tier limits and model names move constantly — these are starting points,
# not guarantees. Check the provider's own docs before relying on them.
# See https://github.com/mnfst/awesome-free-llm-apis for a maintained list.
PROVIDERS: dict[str, dict] = {
    "ollama": {
        "label": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "model": "phi4-mini",
        "embedding_model": "nomic-embed-text",
        # Similarity scale differs per embedding model, so the duplicate
        # threshold belongs to the model, not to the dedup code.
        "embedding_threshold": 0.90,
        "needs_key": False,
        "notes": "Runs on your machine. No limits, no key, works offline.",
    },
    "groq": {
        "label": "Groq",
        # Verified against console.groq.com/docs/models. Groq retires model IDs
        # fairly often — `python cli.py providers` will surface a 404 as a
        # failed connection check.
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "embedding_model": None,
        "needs_key": True,
        "notes": "Fastest, 1000 requests/day. No embeddings endpoint, so those "
                 "fall back to local Ollama.",
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "meta/llama-3.3-70b-instruct",
        "embedding_model": "nvidia/nv-embedqa-e5-v5",
        "needs_key": True,
        "notes": "Serves both chat and embeddings. Needs a free developer account.",
    },
    "openrouter": {
        "label": "OpenRouter",
        # Verified against https://openrouter.ai/api/v1/models. OpenRouter retires
        # free model ids often, so check there if a request 404s.
        "base_url": "https://openrouter.ai/api/v1",
        "model": "nvidia/nemotron-3-super-120b-a12b:free",
        "embedding_model": "liquid/lfm-2.5-embedding-350m:free",
        # Re-measured on 47 real comparisons from four runs, which told a very
        # different story from the synthetic pairs this was first set from.
        # Genuine rewordings score 0.85-0.91 ("an IAM role is an assumable
        # identity" vs "an IAM role is a permission set that can be assumed"),
        # while cards that merely share a topic sit at 0.60-0.80 — the
        # control plane's components score 0.62 against the node's, and the
        # p-value misconception card 0.69 against "what is a p-value".
        # At 0.60 the stage was deleting about half of every run's good cards.
        "embedding_threshold": 0.85,
        "needs_key": True,
        "notes": "One key for chat and embeddings, both free. ~50 requests/day "
                 "until you have bought $10 of credit, then 1000.",
    },
    "sambanova": {
        "label": "SambaNova",
        "base_url": "https://api.sambanova.ai/v1",
        "model": "Meta-Llama-3.3-70B-Instruct",
        "embedding_model": None,
        "needs_key": True,
        "notes": "Daily token budget rather than a request cap.",
    },
    "mistral": {
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
        "embedding_model": "mistral-embed",
        "needs_key": True,
        "notes": "Free tier is monthly credits.",
    },
}

# Reached through google-genai rather than the OpenAI SDK, so it is handled
# on its own path in agents.py / embeddings.py.
NATIVE_PROVIDERS = {"gemini"}
# Reached through the Claude Code CLI, on a subscription login (see llm.py).
CLAUDE = "claude"

# Embeddings computed in-process from an ONNX model: no server, no key, no
# quota, and nothing to be "not running". That is what makes the pipeline
# runnable on a throwaway CI machine, where localhost has no Ollama.
FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
FASTEMBED_THRESHOLD = 0.90

# Used when a model has no measured threshold of its own.
DEFAULT_SEMANTIC_THRESHOLD = 0.90

# The pipeline a run without ANKIGEN_PIPELINE uses: the one there was before
# there could be several.
DEFAULT_PIPELINE = "data-platform"


def provider_names() -> list[str]:
    return sorted(PROVIDERS) + sorted(NATIVE_PROVIDERS) + [CLAUDE]


class PaidModelBlocked(RuntimeError):
    """Raised before a request that would cost money."""


def model_chain(value: str) -> list[str]:
    """Split "best,next,last" into a preference order.

    Free tiers meter per model, so a chain is how you keep writing once the
    best model's allowance is gone: the good one first, weaker ones behind it.
    """
    return [m.strip() for m in (value or "").split(",") if m.strip()]


def ensure_free(provider: str, model: str, allow_paid: bool) -> None:
    """Refuse to call a paid OpenRouter model unless explicitly allowed.

    OpenRouter marks zero-cost models with a `:free` suffix, so the check is
    exact there. Other providers don't encode price in the model id, so their
    own free tiers are the only safeguard.
    """
    if allow_paid or provider != "openrouter":
        return
    if not model.endswith(":free"):
        raise PaidModelBlocked(
            f"Refusing to call OpenRouter model {model!r}: it is not a ':free' model "
            "and would be billed. Pick a free model, or set ALLOW_PAID_MODELS=true "
            "in .env if you really mean to spend credit."
        )


class Settings(BaseSettings):
    # Which provider generates cards. Any key of PROVIDERS, or "gemini".
    llm_provider: str = "ollama"

    # Per-call overrides. Empty means "take it from the preset".
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""

    # The card checker. Empty means "same model that wrote the card", which
    # works but is the weaker arrangement — see resolve_verify. Also a chain.
    verify_provider: str = ""
    verify_model: str = ""

    # Embeddings are configured separately on purpose: most free chat APIs do
    # not serve embeddings, so the usual setup is a hosted model for generation
    # plus local Ollama for embeddings. Empty means "derive from llm_provider".
    embedding_provider: str = ""
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model_name: str = ""

    # Gemini
    google_api_key: str = ""
    # A preference order, not one model. Google meters roughly twenty requests
    # a day per model, and the newest are the busiest — 3.8 answered none of
    # seven requests one morning while 3.6 answered in 1.5s. Best first, and
    # the run walks down the list as models run out or stay busy. Google also
    # retires ids without warning (2.5-flash now 404s for new keys); the
    # current list is at models.list(), and `ankigen providers` surfaces it.
    gemini_model: str = "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash"
    # Always tried after the chain above, whatever it is set to: models with
    # daily allowances of their own, for the days the flash models are all
    # busy or spent — which on the first day of manual runs was by 11:00 UTC.
    # Kept apart from GEMINI_MODEL so that setting a preference order (a CI
    # variable does) cannot quietly remove the safety net. Empty turns it off.
    gemini_fallback_models: str = "gemini-3-flash-preview,gemma-4-31b-it"
    # The checker's, and deliberately not the writer's: a card should not be
    # checked by the model that wrote it.
    verify_fallback_models: str = "gemma-4-26b-a4b-it"
    embedding_model: str = "gemini-embedding-001"

    # Claude, through the Claude Code CLI on a Claude subscription (Pro, Max),
    # never an API key: `claude setup-token` makes the CLAUDE_CODE_OAUTH_TOKEN
    # it runs on in CI, and a local `claude` login works as it is. Best first.
    claude_model: str = "claude-opus-5-5,claude-sonnet-5"

    # Where every request goes once the provider above cannot answer at all:
    # the subscription's usage limit reached, no login, the CLI missing. Set
    # to "gemini", a day with no Claude left carries on on the free tier.
    fallback_provider: str = ""
    # The checker's chain on that provider, since VERIFY_MODEL names the main
    # provider's checker. Empty means the fallback's own writing chain.
    fallback_verify_model: str = ""

    # Retained so existing .env files and the Ollama defaults keep working.
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "phi4-mini"
    ollama_embedding_model: str = "nomic-embed-text"

    # AnkiWeb, for pushing cards straight into the collection instead of
    # exporting a package to import by hand. A key is preferred to a password:
    # `ankigen push --login` trades one for the other, and changing your
    # AnkiWeb password invalidates it.
    ankiweb_username: str = ""
    ankiweb_password: str = ""
    ankiweb_key: str = ""
    ankiweb_endpoint: str = ""      # a self-hosted sync server, if you run one

    # Guard against accidentally billing an OpenRouter key.
    allow_paid_models: bool = False

    # Cosine similarity above which two cards count as duplicates.
    # 0 means "use the embedding model's own default" (see PROVIDERS).
    semantic_threshold: float = 0.0

    # --- pipeline ---
    # Live Anki collection. The pipeline only ever reads a snapshot of it.
    anki_collection_path: str = _default_collection()
    # Which of pipelines/<id>/ this run is. Empty: the default pipeline's
    # profile, with everything in data/ itself, the layout from before there
    # were several.
    ankigen_pipeline: str = ""
    ankigen_pipelines_dir: str = str(BASE_DIR / "pipelines")
    # A profile to use instead of the pipeline's own.
    ankigen_profile: str = ""
    data_dir: str = str(BASE_DIR / "data")
    # Model calls one run may make; 0 is no cap. Every pipeline draws on the
    # same subscription and free tiers, and this is how they share them.
    max_llm_calls: int = 0

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def work_path(self) -> Path:
        """Where this pipeline keeps its warehouse, snapshots and output. The
        AnkiWeb working copy stays in data/ itself: there is one collection."""
        if self.ankigen_pipeline:
            return self.data_path / "pipelines" / self.ankigen_pipeline
        return self.data_path

    def profile_path(self) -> Path:
        if self.ankigen_profile:
            return Path(self.ankigen_profile)
        pipeline = self.ankigen_pipeline or DEFAULT_PIPELINE
        return Path(self.ankigen_pipelines_dir) / pipeline / "profile.yaml"

    # ---------- resolution ----------

    def resolve_llm(self) -> dict:
        """Effective chat config: {provider, base_url, api_key, model, needs_key}."""
        name = (self.llm_provider or "ollama").strip().lower()

        if name == CLAUDE:
            chain = model_chain(self.claude_model)
            return self._with_fallback({
                "provider": name,
                "base_url": None,
                "api_key": "",
                "model": chain[0] if chain else "",
                "models": chain,
                "needs_key": False,
            }, verify=False)

        if name in NATIVE_PROVIDERS:
            chain = model_chain(self.gemini_model)
            chain += [m for m in model_chain(self.gemini_fallback_models) if m not in chain]
            return {
                "provider": name,
                "base_url": None,
                "api_key": self.google_api_key,
                "model": chain[0] if chain else "",
                "models": chain,
                "needs_key": True,
            }

        preset = PROVIDERS.get(name, PROVIDERS["ollama"])

        # Ollama keeps honouring its dedicated settings so old .env files work.
        if name == "ollama":
            base_url = self.llm_base_url or self.ollama_base_url
            model = self.llm_model or self.ollama_model
        else:
            base_url = self.llm_base_url or preset["base_url"]
            model = self.llm_model or preset["model"]

        chain = model_chain(model)
        return {
            "provider": name,
            "base_url": base_url,
            # Local Ollama ignores the key but the OpenAI SDK demands a non-empty one.
            "api_key": self.llm_api_key or ("ollama" if not preset["needs_key"] else ""),
            "model": chain[0] if chain else "",
            "models": chain,
            "needs_key": preset["needs_key"],
        }

    def resolve_verify(self) -> dict:
        """Chat config for the card checker.

        Worth pointing at a different model from the generator. The checker
        exists to catch the generator's factual errors, and a model marking its
        own homework shares its own blind spots — on a sample of real runs the
        drops were overwhelmingly errors one model made and another spotted.
        Free tiers also meter per model, so an independent checker costs
        nothing extra. Falls back to the generation config when unset.
        """
        if not (self.verify_provider or self.verify_model):
            return self.resolve_llm()

        overridden = self.model_copy(update={
            "llm_provider": self.verify_provider or self.llm_provider,
            "llm_model": "" if self.verify_provider else self.verify_model,
            "gemini_model": self.verify_model or self.gemini_model,
            "claude_model": self.verify_model or self.claude_model,
            "gemini_fallback_models": self.verify_fallback_models,
            "fallback_provider": "",        # the checker's own is attached below
        })
        cfg = overridden.resolve_llm()
        if self.verify_model and self.verify_provider:
            chain = model_chain(self.verify_model)
            cfg["model"], cfg["models"] = chain[0], chain
        return self._with_fallback(cfg, verify=True)

    def _with_fallback(self, cfg: dict, verify: bool) -> dict:
        """Attach the fallback provider's config, for the writer or the checker."""
        name = (self.fallback_provider or "").strip().lower()
        if name and name != cfg["provider"]:
            other = self.model_copy(update={
                "llm_provider": name, "fallback_provider": "",
                "verify_provider": "", "verify_model": self.fallback_verify_model,
            })
            cfg["fallback"] = other.resolve_verify() if verify else other.resolve_llm()
        return cfg

    def resolve_embedding(self) -> dict:
        """Effective embedding config, or provider=None when unavailable.

        Falls back to local Ollama when the chat provider serves no embeddings,
        which is the common case on free tiers.
        """
        name = (self.embedding_provider or "").strip().lower()

        if not name:
            chat = (self.llm_provider or "ollama").strip().lower()
            if chat in NATIVE_PROVIDERS:
                name = chat
            elif PROVIDERS.get(chat, {}).get("embedding_model"):
                name = chat
            else:
                # Not Ollama: the fallback should be something that cannot be
                # switched off or rate limited, and that exists on a machine
                # the pipeline has never run on before.
                name = "fastembed"

        if name == "fastembed":
            return {
                "provider": "fastembed",
                "base_url": None,
                "api_key": "",
                "model": self.embedding_model_name or FASTEMBED_MODEL,
                "threshold": self.semantic_threshold or FASTEMBED_THRESHOLD,
            }

        if name in NATIVE_PROVIDERS:
            return {
                "provider": name,
                "base_url": None,
                "api_key": self.google_api_key,
                "model": self.embedding_model_name or self.embedding_model,
                "threshold": self.semantic_threshold or DEFAULT_SEMANTIC_THRESHOLD,
            }

        preset = PROVIDERS.get(name)
        if not preset or not preset.get("embedding_model"):
            return {"provider": None, "base_url": None, "api_key": "", "model": "",
                    "threshold": DEFAULT_SEMANTIC_THRESHOLD}

        if name == "ollama":
            base_url = self.embedding_base_url or self.ollama_base_url
            model = self.embedding_model_name or self.ollama_embedding_model
        else:
            base_url = self.embedding_base_url or preset["base_url"]
            model = self.embedding_model_name or preset["embedding_model"]

        api_key = self.embedding_api_key or (
            self.llm_api_key if name == (self.llm_provider or "").strip().lower() else ""
        )

        return {
            "provider": name,
            "base_url": base_url,
            "api_key": api_key or ("ollama" if not preset["needs_key"] else ""),
            "model": model,
            "threshold": (self.semantic_threshold
                          or preset.get("embedding_threshold")
                          or DEFAULT_SEMANTIC_THRESHOLD),
        }


settings = Settings()
