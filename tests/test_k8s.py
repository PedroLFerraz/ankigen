"""The Kubernetes manifests under infra/k8s/: parseable, and holding the
guarantees the pipeline needs (one run at a time, non-root, no secret in git)."""
from pathlib import Path

import yaml

K8S = Path(__file__).parent.parent / "infra" / "k8s"


def load(name):
    return list(yaml.safe_load_all((K8S / name).read_text(encoding="utf-8")))


def test_every_manifest_parses_and_none_is_a_secret():
    for path in K8S.glob("*.yaml"):
        for doc in load(path.name):
            assert doc["kind"] != "Secret", f"{path.name} would commit a secret"


def test_cronjob_never_overlaps_and_runs_unprivileged():
    (cron,) = load("cronjob.yaml")
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 1000
    assert pod["containers"][0]["args"] == ["run"]
    for c in pod["initContainers"] + pod["containers"]:
        assert c["securityContext"]["allowPrivilegeEscalation"] is False
    claims = [v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"]
              if "persistentVolumeClaim" in v]
    (pvc,) = load("pvc.yaml")
    assert claims == [pvc["metadata"]["name"]]
