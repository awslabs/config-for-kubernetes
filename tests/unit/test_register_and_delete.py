"""Unit tests for the register_resources and delete_resources Lambdas."""
import json
from unittest.mock import MagicMock

from conftest import BUCKET, load_function

HASHES_KEY = "hashes/resource_hashes.json"


def put_json(s3, key, value):
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(value))


def get_json(s3, key):
    return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())


def resource(rid, name):
    return {"resourceId": rid, "resourceInfo": {"K8sResourceKind": "Pod", "K8sResourceName": name}}


def event(batch=0):
    return {"s3Bucket": BUCKET, "executionId": "exec-1", "batchIndex": batch}


# --- register_resources ----------------------------------------------------

def test_register_puts_config_and_merges_hashes(s3):
    m = load_function("register_resources")
    m.cfn_client = MagicMock()
    m.cfn_client.describe_type.return_value = {"DefaultVersionId": "00000003"}
    m.config_client = MagicMock()

    put_json(s3, HASHES_KEY, {"c1/default/Pod/old": "abc"})
    put_json(s3, "executions/exec-1/to_update_0.json", [resource("c1/default/Pod/p1", "p1")])

    result = m.lambda_handler(event(), None)

    assert result == {"status": "success", "batchIndex": 0, "totalToUpdate": 1, "totalUpdated": 1}
    call = m.config_client.put_resource_config.call_args.kwargs
    assert call["ResourceType"] == "AWSCustom::EKS::KubernetesResource"
    assert call["ResourceId"] == "c1/default/Pod/p1"
    assert call["SchemaVersionId"] == "00000003"
    assert json.loads(call["Configuration"])["K8sResourceName"] == "p1"

    hashes = get_json(s3, HASHES_KEY)
    assert set(hashes) == {"c1/default/Pod/old", "c1/default/Pod/p1"}
    _, expected = m.compute_resource_hash(resource("x", "p1")["resourceInfo"])
    assert hashes["c1/default/Pod/p1"] == expected


def test_register_skips_hash_when_config_put_fails(s3):
    m = load_function("register_resources")
    m.cfn_client = MagicMock()
    m.cfn_client.describe_type.return_value = {"DefaultVersionId": "1"}
    m.config_client = MagicMock()
    m.config_client.put_resource_config.side_effect = [RuntimeError("throttled"), None]

    put_json(s3, "executions/exec-1/to_update_0.json", [
        resource("c1/default/Pod/bad", "bad"),
        resource("c1/default/Pod/good", "good"),
    ])

    result = m.lambda_handler(event(), None)

    assert result["totalUpdated"] == 1
    assert set(get_json(s3, HASHES_KEY)) == {"c1/default/Pod/good"}


def test_register_reports_missing_batch(s3):
    m = load_function("register_resources")
    m.cfn_client = MagicMock()
    m.config_client = MagicMock()

    result = m.lambda_handler(event(batch=7), None)

    assert result["status"] == "error"
    m.config_client.put_resource_config.assert_not_called()


# --- delete_resources ------------------------------------------------------

def test_delete_removes_config_items_and_hashes(s3):
    m = load_function("delete_resources")
    m.config_client = MagicMock()

    put_json(s3, HASHES_KEY, {"c1/default/Pod/a": "1", "c1/default/Pod/b": "2"})
    put_json(s3, "executions/exec-1/to_delete_0.json", ["c1/default/Pod/a"])

    result = m.lambda_handler(event(), None)

    assert result == {"status": "success", "batchIndex": 0, "totalToDelete": 1, "totalDeleted": 1}
    m.config_client.delete_resource_config.assert_called_once_with(
        ResourceType="AWSCustom::EKS::KubernetesResource", ResourceId="c1/default/Pod/a"
    )
    assert get_json(s3, HASHES_KEY) == {"c1/default/Pod/b": "2"}


def test_delete_continues_after_a_failed_item(s3):
    m = load_function("delete_resources")
    m.config_client = MagicMock()
    m.config_client.delete_resource_config.side_effect = [RuntimeError("boom"), None]

    put_json(s3, "executions/exec-1/to_delete_0.json", ["c1/default/Pod/a", "c1/default/Pod/b"])

    result = m.lambda_handler(event(), None)

    assert result["totalDeleted"] == 1
    assert get_json(s3, HASHES_KEY) == {}


def test_delete_reports_missing_batch(s3):
    m = load_function("delete_resources")
    m.config_client = MagicMock()

    assert m.lambda_handler(event(batch=3), None)["status"] == "error"
