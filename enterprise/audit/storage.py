"""MinIO screenshot storage for audit compliance.

Handles:
- Uploading before/after screenshots with structured object keys
- Generating presigned URLs for temporary access
- Auto-creating monthly buckets (finrpa-audit-{YYYYMM})
"""

import logging
import uuid
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DEFAULT_PRESIGN_EXPIRY = timedelta(hours=1)


def generate_object_key(
    org_id: str,
    task_id: str,
    action_index: int,
    phase: str,  # "before" or "after"
) -> str:
    """Generate a structured MinIO object key for audit screenshots.

    Format: audit/{org_id}/{task_id}/{index}_{phase}_{uuid}.png
    """
    uid = uuid.uuid4().hex[:12]
    return f"audit/{org_id}/{task_id}/{action_index}_{phase}_{uid}.png"


def get_bucket_name(dt: datetime | None = None) -> str:
    """Get the monthly audit bucket name.

    Format: finrpa-audit-{YYYYMM}
    """
    if dt is None:
        dt = datetime.utcnow()
    return f"finrpa-audit-{dt.strftime('%Y%m')}"


async def ensure_bucket_exists(minio_client, bucket_name: str) -> bool:
    """Create the bucket if it doesn't exist.

    Args:
        minio_client: An async-compatible MinIO client wrapper.
        bucket_name: The bucket name to ensure.

    Returns:
        True if bucket was created, False if it already existed.
    """
    try:
        exists = await minio_client.bucket_exists(bucket_name)
        if not exists:
            await minio_client.make_bucket(bucket_name)
            logger.info("Created audit bucket: %s", bucket_name)
            return True
        return False
    except Exception as e:
        logger.error("Failed to ensure bucket %s: %s", bucket_name, e)
        raise


async def upload_screenshot(
    minio_client,
    bucket_name: str,
    object_key: str,
    data: bytes,
    content_type: str = "image/png",
) -> str:
    """Upload a screenshot to MinIO.

    Args:
        minio_client: An async-compatible MinIO client wrapper.
        bucket_name: The target bucket.
        object_key: The object key (path within bucket).
        data: The screenshot bytes.
        content_type: MIME type.

    Returns:
        The object key on success.
    """
    try:
        await minio_client.put_object(
            bucket_name=bucket_name,
            object_name=object_key,
            data=data,
            length=len(data),
            content_type=content_type,
        )
        logger.info("Uploaded screenshot: %s/%s (%d bytes)", bucket_name, object_key, len(data))
        return object_key
    except Exception as e:
        logger.error("Failed to upload screenshot %s/%s: %s", bucket_name, object_key, e)
        raise


async def get_presigned_url(
    minio_client,
    bucket_name: str,
    object_key: str,
    expiry: timedelta | None = None,
) -> str:
    """Generate a presigned URL for temporary screenshot access.

    Args:
        minio_client: An async-compatible MinIO client wrapper.
        bucket_name: The bucket name.
        object_key: The object key.
        expiry: URL validity duration. Defaults to 1 hour.

    Returns:
        Presigned URL string.
    """
    if expiry is None:
        expiry = DEFAULT_PRESIGN_EXPIRY

    try:
        url = await minio_client.presigned_get_object(
            bucket_name=bucket_name,
            object_name=object_key,
            expires=expiry,
        )
        return url
    except Exception as e:
        logger.error("Failed to generate presigned URL for %s/%s: %s", bucket_name, object_key, e)
        raise
