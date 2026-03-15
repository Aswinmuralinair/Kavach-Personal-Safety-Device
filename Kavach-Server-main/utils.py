"""
utils.py  —  Kavach Server
File saving and SHA-256 hash helpers.

Changes from original:
  + compute_sha256(path)            — compute hash of a file on disk
  + verify_file_hash(path, expected) — compare computed hash against expected
"""

import os
import uuid
import hashlib
import logging
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

# Files with these extensions are accepted.
# All others are rejected to prevent malicious uploads.
ALLOWED_EXTENSIONS = {
    '.h264', '.mp4', '.avi', '.mov',    # video
    '.jpg', '.jpeg', '.png',            # images
    '.txt', '.json', '.log',            # text evidence / logs
    '.sha256',                          # companion hash files
}


def save_file_safe(file_obj, upload_dir: str) -> str | None:
    """
    Save an uploaded file to upload_dir safely.

    Security measures:
      - secure_filename() strips path traversal characters
      - Extension whitelist prevents uploading .py / .sh / .exe etc.
      - UUID prefix prevents filename collisions

    Returns the absolute path of the saved file, or None if rejected.
    """
    if not file_obj or not file_obj.filename:
        logger.warning("[Utils] Empty file object or filename — skipped.")
        return None

    original_name = secure_filename(file_obj.filename)
    _, ext = os.path.splitext(original_name.lower())

    if ext not in ALLOWED_EXTENSIONS:
        logger.warning(
            "[Utils] Rejected file '%s' — extension '%s' not allowed.",
            original_name, ext
        )
        return None

    # Prepend UUID to avoid collisions when the same filename is uploaded
    # multiple times (e.g. "evidence_sample.txt" from every device)
    unique_name = f"{uuid.uuid4().hex[:8]}_{original_name}"
    save_path   = os.path.join(upload_dir, unique_name)

    file_obj.save(save_path)
    file_size = os.path.getsize(save_path)
    logger.info(
        "[Utils] Saved: %s  (%d bytes)", unique_name, file_size
    )
    return save_path


def compute_sha256(file_path: str) -> str:
    """
    Compute the SHA-256 hex digest of a file.
    Reads in 64 KB chunks — safe for large video files without RAM spike.
    Returns the lowercase hex string (64 characters).
    """
    h = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            while chunk := f.read(65536):
                h.update(chunk)
        digest = h.hexdigest()
        logger.debug("[Utils] SHA-256 of %s: %s", os.path.basename(file_path), digest)
        return digest
    except FileNotFoundError:
        logger.error("[Utils] compute_sha256: file not found: %s", file_path)
        return ""
    except Exception as exc:
        logger.error("[Utils] compute_sha256 error: %s", exc)
        return ""


def verify_file_hash(file_path: str, expected_hash: str) -> bool:
    """
    Verify the SHA-256 of file_path matches expected_hash.
    Both compared lowercase for safety.

    Returns True  if hashes match (file is intact).
    Returns False if they differ (file may be corrupted or tampered).
    Returns False if the file does not exist.
    """
    if not os.path.exists(file_path):
        logger.warning("[Utils] verify_file_hash: file not found: %s", file_path)
        return False

    computed = compute_sha256(file_path)
    if not computed:
        return False

    match = computed.lower() == expected_hash.lower()
    if match:
        logger.info(
            "[Utils] Hash VERIFIED: %s", os.path.basename(file_path)
        )
    else:
        logger.warning(
            "[Utils] Hash MISMATCH: %s\n  expected: %s\n  computed: %s",
            os.path.basename(file_path),
            expected_hash.lower(),
            computed.lower(),
        )
    return match