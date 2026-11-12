"""k8s 매니페스트 (k8s 설계 §4.5): 자원 예산과 필수 설정."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent / "deploy" / "k8s"


def _docs():
    return [d for f in ROOT.glob("*.yaml") if f.name not in ("kustomization.yaml", "plant-sim-secret.example.yaml")
            for d in yaml.safe_load_all(f.read_text()) if d]


def test_k8s_manifests_build_and_fit_budget():
    docs = {d["kind"]: d for d in _docs()}
    assert set(docs) >= {"Deployment", "Service", "PersistentVolumeClaim", "Ingress"}
    dep = docs["Deployment"]["spec"]
    assert dep["replicas"] == 1 and dep["strategy"]["type"] == "Recreate"
    c = dep["template"]["spec"]["containers"][0]
    res = c["resources"]
    assert res["requests"] == {"cpu": "200m", "memory": "512Mi"} and res["limits"] == {"cpu": "500m", "memory": "1Gi"}
    assert {p for p in ("startupProbe", "readinessProbe", "livenessProbe")} <= set(c)
    assert c["startupProbe"]["failureThreshold"] * c["startupProbe"]["periodSeconds"] >= 1800
    assert "--with-simulators" in c["args"] and "config/plants.k8s.yaml" in " ".join(c["args"])
    env = {e["name"]: e for e in c["env"]}
    assert env["ROOT_PATH"]["value"] == "/plant-sim" and "secretKeyRef" in env["INGEST_TOKEN"]["valueFrom"]
    assert "SIM_SCENARIO_DIR" not in env                                   # 시나리오 Secret 은 지웠다
    assert [v["name"] for v in dep["template"]["spec"]["volumes"]] == ["data"]
    assert [m["name"] for m in c["volumeMounts"]] == ["data"]
    assert docs["PersistentVolumeClaim"]["spec"]["storageClassName"] == "ebs-sc"
    ing = docs["Ingress"]
    rule = ing["spec"]["rules"][0]
    assert rule["host"] == "u4-2.skala-us.com" and rule["http"]["paths"][0]["path"].startswith("/plant-sim")
    ann = ing["metadata"]["annotations"]
    assert ann["nginx.ingress.kubernetes.io/proxy-buffering"] == "off" and ann["nginx.ingress.kubernetes.io/rewrite-target"] == "/$2"
    assert "LoadBalancer" not in docs["Service"]["spec"].get("type", "ClusterIP")
    if shutil.which("kubectl"):
        subprocess.run(["kubectl", "kustomize", str(ROOT)], check=True, capture_output=True)
    else:
        pytest.skip("kubectl not installed; kustomize build skipped")
