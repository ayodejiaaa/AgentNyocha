"""File storage, behind one interface so a job's uploaded PDFs and
generated snapshot images work identically whether they're sitting on
local disk (dev) or in an S3 bucket (production - AWS S3, or any S3-compatible
service such as Cloudflare R2). Selected by config.STORAGE_BACKEND.

Deliberately NOT served from a public URL in either mode - these are real
people's academic records. api/main.py's /files/{key} route proxies bytes
through the authenticated API instead; see its docstring.
"""

from pathlib import Path

from . import config


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def put(self, key: str, local_path: str):
        dst = self.root / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(Path(local_path).read_bytes())

    def put_bytes(self, key: str, data: bytes):
        dst = self.root / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)

    def get_bytes(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def delete(self, key: str):
        (self.root / key).unlink(missing_ok=True)

    def get_local_path(self, key: str) -> str:
        """Only local storage can hand back a real filesystem path - the
        worker uses this to point PyMuPDF straight at an uploaded PDF
        without a redundant copy. The S3 backend has no equivalent (see
        extraction.py, which downloads to a temp file instead)."""
        return str(self.root / key)


class S3Storage:
    def __init__(self):
        import boto3
        self.bucket = config.S3_BUCKET
        self.client = boto3.client(
            "s3", endpoint_url=config.S3_ENDPOINT_URL or None,
            # Blank keys mean "none": boto3 then uses the server's own IAM role
            # (what you want on AWS - no long-lived keys on the box). Passing an
            # empty string instead would be used as the key and fail.
            aws_access_key_id=config.S3_ACCESS_KEY or None, aws_secret_access_key=config.S3_SECRET_KEY or None,
            region_name=config.S3_REGION or None,
        )

    def put(self, key: str, local_path: str):
        self.client.upload_file(local_path, self.bucket, key)

    def put_bytes(self, key: str, data: bytes):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str):
        self.client.delete_object(Bucket=self.bucket, Key=key)


def get_storage():
    if config.STORAGE_BACKEND == "s3":
        return S3Storage()
    return LocalStorage(config.LOCAL_DATA_DIR / "files")
