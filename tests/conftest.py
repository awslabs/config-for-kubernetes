"""Shared fixtures for the Lambda function tests.

Each Lambda is a standalone ``index.py`` that reads its configuration from
environment variables and creates its boto3 clients at import time. The
helpers below set that environment, start moto, and import a fresh copy of
the module so every test sees clean module-level state.
"""
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

REPO_ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS_DIR = REPO_ROOT / "source" / "functions"

BUCKET = "c4k-test-bucket"
LAMBDA_ROLE = "arn:aws:iam::123456789012:role/DiscoverResourcesRole"

DISCOVER_ENV = {
    "S3_BUCKET": BUCKET,
    "LAMBDA_ROLE": LAMBDA_ROLE,
    "RESOURCE_KINDS": "Pod,Deployment,ConfigMap",
    "LABEL_SELECTOR": "",
    "INCLUDE_NAMESPACES": "",
    "EXCLUDE_NAMESPACES": "kube-system",
    "INCLUDE_RESOURCE_FIELDS": "",
    "EXCLUDE_RESOURCE_FIELDS": "metadata.managedFields",
}


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    """Fake credentials and a region so no test can reach a real account."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def s3():
    """A moto S3 with the solution bucket already created."""
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket=BUCKET)
        yield client


def load_function(name):
    """Import ``source/functions/<name>/index.py`` as a fresh module."""
    path = FUNCTIONS_DIR / name / "index.py"
    module_name = f"c4k_{name}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_config_client(existing_ids=()):
    """A stand-in for the AWS Config client.

    moto does not model custom resource types, so Config is a MagicMock that
    reports ``existing_ids`` as already-recorded resources.
    """
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"resourceIdentifiers": [{"resourceId": rid} for rid in existing_ids]}
    ]
    client.get_paginator.return_value = paginator
    return client


@pytest.fixture
def discover_module(s3, monkeypatch):
    """Factory: import the discover Lambda with DISCOVER_ENV plus overrides."""

    def _load(**overrides):
        for key, value in {**DISCOVER_ENV, **overrides}.items():
            monkeypatch.setenv(key, value)
        return load_function("discover_resources")

    return _load
