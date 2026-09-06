from __future__ import annotations

import subprocess

from pydantic import BaseModel, ConfigDict, Field


class ComposeVolume(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    target: str


class ComposeDependency(BaseModel):
    model_config = ConfigDict(frozen=True)

    condition: str


class ComposeService(BaseModel):
    model_config = ConfigDict(frozen=True)

    command: list[str] | None = None
    depends_on: dict[str, ComposeDependency] = Field(default_factory=dict)
    environment: dict[str, str] = Field(default_factory=dict)
    volumes: list[ComposeVolume] = Field(default_factory=list)


class ComposeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    services: dict[str, ComposeService]


def test_webchat_documents_share_persistent_storage_with_download_backend() -> None:
    # Given: the complete web-chat Docker configuration.
    rendered = subprocess.run(
        ["docker", "compose", "--profile", "kafka", "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )

    # When: the document producer and download backend storage are resolved.
    config = ComposeConfig.model_validate_json(rendered.stdout)
    gateway = config.services["gateway"]
    doc_worker = config.services["doc-worker"]
    gateway_doc_volume = next(
        volume for volume in gateway.volumes if volume.target == "/var/lib/ainxt/docs"
    )
    worker_doc_volume = next(
        volume for volume in doc_worker.volumes if volume.target == "/var/lib/ainxt/docs"
    )

    # Then: both processes use the same persistent path and the worker consumes doc jobs.
    assert gateway.environment["AINXT_DOC_STORAGE_DIR"] == "/var/lib/ainxt/docs"
    assert doc_worker.environment["AINXT_DOC_STORAGE_DIR"] == "/var/lib/ainxt/docs"
    assert gateway_doc_volume.source == worker_doc_volume.source
    assert doc_worker.command == ["python", "workers/start_workers.py", "--doc", "--n", "1"]
    assert gateway.depends_on["doc-worker"].condition == "service_started"
