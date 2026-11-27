"""Object storage for uploaded images: Cloudflare R2 (S3 API) or Cloudinary.

New uploads go to R2 when its keys are set (STORAGE_BACKEND=cloudinary forces Cloudinary). Each file record remembers
its own provider, so old Cloudinary files keep working while scripts/migrate_to_r2.py moves them across.

R2 layout. Two buckets, so a public domain can never expose private artwork:
  R2_BUCKET (no public access)          private/<owner>/<id>.<ext> design artwork, only through this backend (owner/admin checks)
                                        backups/<file> daily database dumps (scripts/backup_to_r2.py)
  R2_PUBLIC_BUCKET + R2_PUBLIC_URL      public/<owner>/<id>.<ext> shop/product photos, served straight from the custom domain
  (optional; unset = public photos also go in R2_BUCKET and are streamed by the backend)
  thumbs/<w>/<key>.webp                 resized variants, next to their original, made on first request
"""
import io
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

THUMB_WIDTHS = (240, 480, 960)
_client = None


def r2_ready() -> bool:
    return all(os.environ.get(k) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"))


def public_base() -> Optional[str]:
    """e.g. https://cdn.dropforge.in, the custom domain of R2_PUBLIC_BUCKET. None: public files are streamed by the backend."""
    v = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip("/")
    return v if v.startswith("https://") and os.environ.get("R2_PUBLIC_BUCKET") else None


def bucket(public: bool = False) -> str:
    return os.environ["R2_PUBLIC_BUCKET"] if public and public_base() else os.environ["R2_BUCKET"]


def upload_backend() -> Optional[str]:
    from files_routes import cloudinary_ready

    forced = os.environ.get("STORAGE_BACKEND", "").strip().lower()
    if forced != "cloudinary" and r2_ready():
        return "r2"
    if cloudinary_ready():
        return "cloudinary"
    return None


def client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=10, read_timeout=60),
        )
    return _client


def r2_put(b: str, key: str, data: bytes, content_type: str, cache: str = "public, max-age=31536000, immutable"):
    client().put_object(Bucket=b, Key=key, Body=data, ContentType=content_type, CacheControl=cache)


def r2_get(b: str, key: str) -> bytes:
    return client().get_object(Bucket=b, Key=key)["Body"].read()


def r2_delete(b: str, keys: list):
    keys = [k for k in keys if k]
    for i in range(0, len(keys), 1000):
        client().delete_objects(Bucket=b, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})


def r2_list(b: str, prefix: str) -> list:
    out, token = [], None
    while True:
        kw = {"Bucket": b, "Prefix": prefix, **({"ContinuationToken": token} if token else {})}
        page = client().list_objects_v2(**kw)
        out += page.get("Contents", [])
        if not page.get("IsTruncated"):
            return out
        token = page["NextContinuationToken"]


def thumb_key(key: str, w: int) -> str:
    return f"thumbs/{w}/{key}.webp"


def make_thumb(data: bytes, w: int) -> Optional[bytes]:
    """WEBP no wider than w (aspect kept, transparency kept). None for animated GIFs or anything Pillow can't read."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 50_000_000  # refuse decompression bombs
    try:
        im = Image.open(io.BytesIO(data))
        if getattr(im, "is_animated", False):
            return None
        im.load()
        if im.width <= w:
            w = im.width
        im.thumbnail((w, w * 4))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB")
        out = io.BytesIO()
        im.save(out, "WEBP", quality=82, method=4)
        return out.getvalue()
    except Exception as e:
        logger.info("thumbnail failed: %s", e)
        return None
