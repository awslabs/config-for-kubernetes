"""End-to-end test of discover -> register -> delete against a real cluster.

Runs only when C4K_INTEGRATION=1 and KUBECONFIG points at a disposable
cluster (CI uses kind). The test creates a namespace, churns Pods,
Deployments and ConfigMaps in it, and checks that each discovery run
reports exactly the resources that were added, changed or removed.

EKS, AWS Config and CloudFormation are replaced by fakes; S3 is moto. The
Kubernetes side is the real API server, which is the part a dependency bump
of the ``kubernetes`` client is most likely to break.
"""
import json
import os
import subprocess
import time
import uuid
from unittest.mock import MagicMock

import pytest
from kubernetes import client, config
from kubernetes.dynamic import DynamicClient

from conftest import BUCKET, fake_config_client, load_function

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("C4K_INTEGRATION") != "1", reason="set C4K_INTEGRATION=1 to run"),
]

CLUSTER = "kind-c4k"
NS = f"c4k-it-{uuid.uuid4().hex[:6]}"


def kubectl(*args, stdin=None):
    subprocess.run(["kubectl", *args], input=stdin, text=True, check=True, capture_output=True)


@pytest.fixture(scope="module")
def namespace():
    kubectl("create", "namespace", NS)
    yield NS
    kubectl("delete", "namespace", NS, "--wait=false")


@pytest.fixture
def pipeline(discover_module, s3, monkeypatch):
    """Wire the three Lambdas to the live cluster and a shared fake Config."""
    recorded = {}  # resourceId -> Configuration, i.e. what AWS Config holds

    def make_discover():
        m = discover_module(
            RESOURCE_KINDS="Pod,Deployment,ConfigMap",
            INCLUDE_NAMESPACES=NS,
            EXCLUDE_NAMESPACES="",
            # status and resourceVersion move on their own; keep the test about spec/metadata changes
            EXCLUDE_RESOURCE_FIELDS="status,metadata.resourceVersion,metadata.managedFields,"
                                    "metadata.generation,metadata.annotations",
        )
        m.eks_client = MagicMock()
        m.eks_client.get_paginator.return_value.paginate.return_value = [{"clusters": [CLUSTER]}]
        m.eks_client.describe_cluster.return_value = {
            "cluster": {"status": "ACTIVE", "accessConfig": {"authenticationMode": "API"}, "tags": {}}
        }
        m.config_client = fake_config_client(sorted(recorded))
        monkeypatch.setattr(m, "ensure_eks_access", lambda name: None)

        def live_client(name):
            config.load_kube_config()
            return DynamicClient(client.ApiClient())

        monkeypatch.setattr(m, "configure_kubernetes_client", live_client)
        return m

    register = load_function("register_resources")
    register.cfn_client = MagicMock()
    register.cfn_client.describe_type.return_value = {"DefaultVersionId": "1"}
    register.config_client = MagicMock()
    register.config_client.put_resource_config.side_effect = (
        lambda **kw: recorded.__setitem__(kw["ResourceId"], json.loads(kw["Configuration"]))
    )

    delete = load_function("delete_resources")
    delete.config_client = MagicMock()
    delete.config_client.delete_resource_config.side_effect = lambda **kw: recorded.pop(kw["ResourceId"])

    run_counter = iter(range(1000))

    def run():
        """One state machine execution; returns (updated ids, deleted ids)."""
        execution = f"exec-{next(run_counter)}"
        out = make_discover().lambda_handler({"executionId": execution}, None)
        updated, deleted = set(), set()
        for batch in out["toRegisterList"]:
            body = s3.get_object(Bucket=BUCKET, Key=f"executions/{execution}/to_update_{batch}.json")["Body"].read()
            updated |= {r["resourceId"] for r in json.loads(body)}
            register.lambda_handler({"s3Bucket": BUCKET, "executionId": execution, "batchIndex": batch}, None)
        for batch in out["toDeleteList"]:
            body = s3.get_object(Bucket=BUCKET, Key=f"executions/{execution}/to_delete_{batch}.json")["Body"].read()
            deleted |= set(json.loads(body))
            delete.lambda_handler({"s3Bucket": BUCKET, "executionId": execution, "batchIndex": batch}, None)
        return updated, deleted

    run.recorded = recorded
    return run


def rid(kind, name):
    return f"{CLUSTER}/{NS}/{kind}/{name}"


def pod_ids():
    out = subprocess.run(
        ["kubectl", "get", "pods", "-n", NS, "-o", "jsonpath={.items[*].metadata.name}"],
        text=True, check=True, capture_output=True,
    ).stdout.split()
    return {rid("Pod", p) for p in out}


def wait_for(predicate, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(2)
    raise AssertionError("timed out waiting for cluster state")


def test_resource_lifecycle_is_mirrored_to_config(namespace, pipeline):
    # 1. Create: a ConfigMap and a 2-replica Deployment
    kubectl("-n", NS, "create", "configmap", "app-config", "--from-literal=mode=a")
    kubectl("-n", NS, "create", "deployment", "web", "--image=registry.k8s.io/pause:3.10", "--replicas=2")
    kubectl("-n", NS, "rollout", "status", "deployment/web", "--timeout=120s")
    wait_for(lambda: len(pod_ids()) == 2)
    pods_v1 = pod_ids()

    updated, deleted = pipeline()
    assert updated == {rid("ConfigMap", "app-config"), rid("Deployment", "web"), rid("ConfigMap", "kube-root-ca.crt")} | pods_v1
    assert deleted == set()
    assert pipeline.recorded[rid("ConfigMap", "app-config")]["K8sItem"]["data"] == {"mode": "a"}

    # 2. No change: nothing to register or delete
    assert pipeline() == (set(), set())

    # 3. Modify: only the touched ConfigMap is re-registered
    kubectl("-n", NS, "patch", "configmap", "app-config", "--type=merge", "-p", '{"data":{"mode":"b"}}')
    updated, deleted = pipeline()
    assert updated == {rid("ConfigMap", "app-config")}
    assert deleted == set()
    assert pipeline.recorded[rid("ConfigMap", "app-config")]["K8sItem"]["data"] == {"mode": "b"}

    # 4. Scale down: removed Pods are deleted from Config, the Deployment is updated
    kubectl("-n", NS, "scale", "deployment", "web", "--replicas=1")
    wait_for(lambda: len(pod_ids()) == 1)
    updated, deleted = pipeline()
    assert rid("Deployment", "web") in updated
    assert deleted == pods_v1 - pod_ids()
    assert len(deleted) == 1

    # 5. Delete: everything we created disappears from Config
    kubectl("-n", NS, "delete", "deployment", "web", "--wait=true")
    kubectl("-n", NS, "delete", "configmap", "app-config")
    wait_for(lambda: not pod_ids())
    updated, deleted = pipeline()
    assert updated == set()
    assert rid("ConfigMap", "app-config") in deleted
    assert rid("Deployment", "web") in deleted
    assert set(pipeline.recorded) == {rid("ConfigMap", "kube-root-ca.crt")}
