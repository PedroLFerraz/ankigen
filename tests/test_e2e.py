"""The installed `ankigen run` end to end over a tiny collection, fake model and embeddings.

Needs `pip install .`: the subprocess imports the installed ankigen, not src/.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
from conftest import RUN_DATE

# A subprocess does not see the in-process fakes, so patch them in before main().
BOOT = """
import os, sys
sys.path.insert(0, os.environ["ANKIGEN_TESTS_DIR"])
from conftest import FakeLLM, fake_vector
import ankigen.llm, ankigen.dedup, ankigen.cli
ankigen.llm.call_json = FakeLLM()
ankigen.dedup.Embedder._embed_batch = lambda self, texts: [fake_vector(t) for t in texts]
ankigen.cli.main()
"""


def test_the_installed_cli_runs_a_whole_day(tmp_path, modern_collection, profile):
    with open(tmp_path / "profile.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(profile.model_dump(), f)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(
        ANKI_COLLECTION_PATH=str(modern_collection),
        DATA_DIR=str(tmp_path / "data"),
        ANKIGEN_PROFILE=str(tmp_path / "profile.yaml"),
        LLM_PROVIDER="ollama",
        EMBEDDING_PROVIDER="fastembed",
        FALLBACK_PROVIDER="",
        ANKIGEN_PIPELINE="",
        ANKIGEN_TESTS_DIR=str(Path(__file__).parent),
    )
    proc = subprocess.run(
        [sys.executable, "-c", BOOT, "run", "--date", str(RUN_DATE)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    out = tmp_path / "data" / "out" / str(RUN_DATE)
    assert (out / f"ankigen_{RUN_DATE}.apkg").exists()
    report = json.loads((out / "run_report.json").read_text())
    assert report["stages"]
    assert all(s["status"] == "success" for s in report["stages"])
