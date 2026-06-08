"""Artifact store — saves the run video (MP4) and trajectory JSON to MinIO
(S3-compatible). Returns an object ref the DB/UI can resolve.

Keys are namespaced by tenant + task so a tenant can only ever address its own
artifacts: `<tenant>/<task>/<kind>.<ext>`.
"""

import json
import logging
from typing import Any, Optional

import boto3
from botocore.client import Config

from livellm_agent.config import settings

logger = logging.getLogger(__name__)


class ArtifactStore:
    def __init__(self) -> None:
        self._bucket = settings.artifact_bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=settings.artifact_endpoint,
            aws_access_key_id=settings.artifact_access_key,
            aws_secret_access_key=settings.artifact_secret_key,
            config=Config(signature_version="s3v4"),
        )

    def _key(self, task_id: str, name: str) -> str:
        tenant = settings.tenant_id or "unknown"
        return f"{tenant}/{task_id}/{name}"

    def ensure_bucket(self) -> None:
        try:
            self._s3.head_bucket(Bucket=self._bucket)
        except Exception:
            self._s3.create_bucket(Bucket=self._bucket)

    def put_bytes(self, task_id: str, name: str, data: bytes, content_type: str) -> str:
        key = self._key(task_id, name)
        self._s3.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )
        ref = f"s3://{self._bucket}/{key}"
        logger.info("artifact stored: %s (%d bytes)", ref, len(data))
        return ref

    def put_video(self, task_id: str, mp4: bytes) -> str:
        return self.put_bytes(task_id, "run.mp4", mp4, "video/mp4")

    def put_trajectory(self, task_id: str, trajectory: Any) -> str:
        payload = (
            trajectory.model_dump_json().encode()
            if hasattr(trajectory, "model_dump_json")
            else json.dumps(trajectory, default=str).encode()
        )
        return self.put_bytes(task_id, "trajectory.json", payload, "application/json")


_store: Optional[ArtifactStore] = None


def store() -> ArtifactStore:
    """Lazily build the store so the image boots without artifact config set."""
    global _store
    if _store is None:
        _store = ArtifactStore()
    return _store
