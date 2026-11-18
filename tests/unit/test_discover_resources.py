"""Unit tests for the discover_resources Lambda (Kubernetes and AWS mocked)."""
import json
from unittest.mock import MagicMock

import pytest

from conftest import BUCKET, fake_config_client


class FakeItem:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return json.loads(json.dumps(self._data))


class FakeResource:
    """Mimics a kubernetes DynamicClient resource with paginated get()."""

    def __init__(self, items, namespaced=True, page_size=None):
        self.items = items
        self.namespaced = namespaced
        self.page_size = page_size
        self.calls = []

    def get(self, **params):
        self.calls.append(params)
        size = self.page_size or len(self.items)
        start = int(params.get("_continue") or 0)
        page = self.items[start:start + size]
        nxt = start + size
        response = MagicMock()
        response.items = [FakeItem(i) for i in page]
        response.metadata = {"continue": str(nxt)} if nxt < len(self.items) else {}
        return response


def pod(name, namespace="default", labels=None):
    return {
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels or {},
            "managedFields": [{"manager": "kubectl"}],
        },
        "spec": {"containers": [{"name": "app", "image": "nginx"}]},
    }


# --- pure helpers -----------------------------------------------------------

def test_generate_resource_id(discover_module):
    m = discover_module()
    assert m.generate_resource_id("c1", "Pod", "p1", "ns") == "c1/ns/Pod/p1"
    assert m.generate_resource_id("c1", "Namespace", "ns") == "c1/Namespace/ns"


@pytest.mark.parametrize(
    "namespace,include,exclude,expected",
    [
        ("a", set(), set(), True),
        ("a", {"a"}, set(), True),
        ("b", {"a"}, set(), False),
        ("a", set(), {"a"}, False),
        ("b", set(), {"a"}, True),
    ],
)
def test_should_process_namespace(discover_module, namespace, include, exclude, expected):
    m = discover_module()
    assert m.should_process_namespace(namespace, include, exclude) is expected


def test_filter_resource_fields_include(discover_module):
    m = discover_module()
    item = pod("p1", labels={"app": "x"})
    out = m.filter_resource_fields(item, ["metadata.name", "metadata.labels", "missing.field"], [])
    assert out == {"metadata": {"name": "p1", "labels": {"app": "x"}}}


def test_filter_resource_fields_exclude(discover_module):
    m = discover_module()
    item = pod("p1")
    out = m.filter_resource_fields(item, [], ["metadata.managedFields", "spec"])
    assert "managedFields" not in out["metadata"]
    assert "spec" not in out
    assert out["metadata"]["name"] == "p1"


def test_compute_resource_hash_is_order_independent(discover_module):
    m = discover_module()
    _, h1 = m.compute_resource_hash({"a": 1, "b": 2})
    _, h2 = m.compute_resource_hash({"b": 2, "a": 1})
    assert h1 == h2


# --- configuration validation ----------------------------------------------

def test_import_fails_without_resource_kinds(discover_module):
    with pytest.raises(ValueError):
        discover_module(RESOURCE_KINDS=" , ")


def test_import_fails_with_both_namespace_filters(discover_module):
    with pytest.raises(SystemExit):
        discover_module(INCLUDE_NAMESPACES="a", EXCLUDE_NAMESPACES="b")


# --- pagination and change detection ---------------------------------------

def run_fetch(m, resource, previous_hashes=None, label_selector=""):
    all_resources, ids, batches = [], set(), []
    m.fetch_resources_with_pagination(
        resource, "Pod", label_selector, "c1",
        m.include_namespaces, m.exclude_namespaces,
        m.include_resource_fields, m.exclude_resource_fields,
        all_resources, ids, batches, previous_hashes or {}, "exec-1",
    )
    return all_resources, ids, batches


def test_fetch_follows_pagination_and_filters_namespaces(discover_module, monkeypatch):
    m = discover_module()
    monkeypatch.setattr(m.time, "sleep", lambda _: None)
    resource = FakeResource(
        [pod("p1"), pod("p2", "kube-system"), pod("p3"), pod("p4")], page_size=2
    )
    found, ids, _ = run_fetch(m, resource, label_selector="app=x")

    assert len(resource.calls) == 2
    assert resource.calls[0]["label_selector"] == "app=x"
    assert resource.calls[1]["_continue"] == "2"
    assert ids == {"c1/default/Pod/p1", "c1/default/Pod/p3", "c1/default/Pod/p4"}
    assert all("managedFields" not in r["resourceInfo"]["K8sItem"]["metadata"] for r in found)
    assert found[0]["resourceInfo"]["K8sNamespace"] == "default"


def test_fetch_skips_unchanged_resources(discover_module):
    m = discover_module()
    first, _, _ = run_fetch(m, FakeResource([pod("p1"), pod("p2")]))
    hashes = {r["resourceId"]: m.compute_resource_hash(r["resourceInfo"])[1] for r in first}

    changed = pod("p2", labels={"new": "label"})
    second, ids, _ = run_fetch(m, FakeResource([pod("p1"), changed]), previous_hashes=hashes)

    assert ids == {"c1/default/Pod/p1", "c1/default/Pod/p2"}
    assert [r["resourceId"] for r in second] == ["c1/default/Pod/p2"]


def test_flush_writes_batch_to_s3(discover_module, s3, monkeypatch):
    m = discover_module()
    monkeypatch.setattr(m, "BATCH_SIZE", 2)
    found, _, batches = run_fetch(m, FakeResource([pod("p1"), pod("p3"), pod("p4")]))

    assert batches == [0]
    assert len(found) == 1  # third pod waits for the final flush
    body = s3.get_object(Bucket=BUCKET, Key="executions/exec-1/to_update_0.json")["Body"].read()
    assert [r["resourceId"] for r in json.loads(body)] == ["c1/default/Pod/p1", "c1/default/Pod/p3"]


# --- cluster selection -----------------------------------------------------

def cluster(status="ACTIVE", auth="API", tags=None):
    return {"cluster": {"status": status, "accessConfig": {"authenticationMode": auth}, "tags": tags or {}}}


@pytest.mark.parametrize(
    "desc,env,expected",
    [
        (cluster(), {}, True),
        (cluster(status="CREATING"), {}, False),
        (cluster(auth="CONFIG_MAP"), {}, False),
        (cluster(auth="API_AND_CONFIG_MAP"), {}, True),
        (cluster(tags={"skip": "1"}), {"EXCLUDE_CLUSTER_TAG": "skip"}, False),
        (cluster(), {"INCLUDE_CLUSTER_TAG": "c4k"}, False),
        (cluster(tags={"c4k": "1"}), {"INCLUDE_CLUSTER_TAG": "c4k"}, True),
    ],
)
def test_should_process_cluster(discover_module, desc, env, expected):
    m = discover_module(**env)
    m.eks_client = MagicMock()
    m.eks_client.describe_cluster.return_value = desc
    assert m.should_process_cluster("c1") is expected


def test_ensure_eks_access_creates_entry_when_missing(discover_module, monkeypatch):
    m = discover_module()
    eks = MagicMock()

    class NotFound(Exception):
        pass

    eks.exceptions.ResourceNotFoundException = NotFound
    eks.describe_access_entry.side_effect = NotFound()
    m.eks_client = eks
    monkeypatch.setattr(m.time, "sleep", lambda _: None)

    m.ensure_eks_access("c1")

    eks.create_access_entry.assert_called_once()
    policy = eks.associate_access_policy.call_args.kwargs
    assert policy["policyArn"].endswith("AmazonEKSViewPolicy")
    assert policy["accessScope"] == {"type": "cluster"}


# --- handler end to end ----------------------------------------------------

def test_lambda_handler_registers_new_and_deletes_stale(discover_module, s3, monkeypatch):
    m = discover_module()
    m.eks_client = MagicMock()
    m.eks_client.get_paginator.return_value.paginate.return_value = [{"clusters": ["c1"]}]
    m.eks_client.describe_cluster.return_value = cluster()
    m.config_client = fake_config_client(["c1/default/Pod/gone", "c1/default/Pod/p1"])
    monkeypatch.setattr(m, "ensure_eks_access", lambda name: None)

    dyn = MagicMock()
    dyn.resources.get.side_effect = lambda kind: FakeResource([pod("p1")] if kind == "Pod" else [])
    monkeypatch.setattr(m, "configure_kubernetes_client", lambda name: dyn)

    result = m.lambda_handler({"executionId": "exec-1"}, None)

    assert result == {"toRegisterList": [0], "toDeleteList": [0], "s3Bucket": BUCKET, "executionId": "exec-1"}
    to_delete = json.loads(s3.get_object(Bucket=BUCKET, Key="executions/exec-1/to_delete_0.json")["Body"].read())
    assert to_delete == ["c1/default/Pod/gone"]


def test_lambda_handler_fails_when_kind_cannot_be_listed(discover_module, monkeypatch):
    m = discover_module()
    m.eks_client = MagicMock()
    m.eks_client.get_paginator.return_value.paginate.return_value = [{"clusters": ["c1"]}]
    m.eks_client.describe_cluster.return_value = cluster()
    m.config_client = fake_config_client()
    monkeypatch.setattr(m, "ensure_eks_access", lambda name: None)
    dyn = MagicMock()
    dyn.resources.get.side_effect = RuntimeError("no such kind")
    monkeypatch.setattr(m, "configure_kubernetes_client", lambda name: dyn)

    with pytest.raises(Exception, match="Error listing"):
        m.lambda_handler({"executionId": "exec-1"}, None)


def test_get_bearer_token_format(discover_module):
    m = discover_module()
    token = m.get_bearer_token("c1")
    assert token.startswith("k8s-aws-v1.")
    assert "=" not in token
