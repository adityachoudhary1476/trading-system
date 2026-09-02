"""Broker connection and token management."""
from __future__ import annotations

import base64
import logging
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from config import get_settings

logger = logging.getLogger(__name__)

# Must match the JavaScript implementation exactly
SALT = b"finova-upstox-token-v1"
KEY_LENGTH = 32
IV_LENGTH = 16
TAG_LENGTH = 16


def _derive_key() -> bytes:
    """Derive the encryption key using scrypt (must match Node.js scryptSync)."""
    settings = get_settings()
    if not settings.upstox_token_encryption_key:
        raise RuntimeError("UPSTOX_TOKEN_ENCRYPTION_KEY is not configured")

    # Node.js scryptSync uses scrypt with N=16384, r=8, p=1
    kdf = Scrypt(
        salt=SALT,
        length=KEY_LENGTH,
        n=2**14,
        r=8,
        p=1,
    )
    key = kdf.derive(settings.upstox_token_encryption_key.encode("utf-8"))
    return key


def decrypt_token(blob: str) -> str:
    """
    Decrypt an Upstox access token.

    Blob format: base64(iv):base64(tag):base64(ciphertext)
    """
    key = _derive_key()
    parts = blob.split(":")
    if len(parts) != 3:
        raise ValueError("Malformed encrypted token blob")

    iv = base64.b64decode(parts[0])
    tag = base64.b64decode(parts[1])
    ciphertext = base64.b64decode(parts[2])

    if len(iv) != IV_LENGTH:
        raise ValueError(f"Invalid IV length: {len(iv)}")
    if len(tag) != TAG_LENGTH:
        raise ValueError(f"Invalid tag length: {len(tag)}")

    # AES-GCM decryption
    aesgcm = AESGCM(key)
    # The cryptography library expects tag appended to ciphertext
    plaintext = aesgcm.decrypt(iv, ciphertext + tag, None)
    return plaintext.decode("utf-8")


async def get_upstox_access_token(user_id: str) -> Optional[str]:
    """
    Retrieve and decrypt the user's Upstox access token from Supabase.

    Returns None if no connection exists or decryption fails.
    """
    try:
        from supabase import create_client

        settings = get_settings()
        if not settings.supabase_url or not settings.supabase_service_role_key:
            logger.error("Supabase configuration is missing")
            return None

        sb = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )

        response = (
            sb.table("broker_connections")
            .select("access_token_encrypted")
            .eq("user_id", user_id)
            .eq("provider", "upstox")
            .maybe_single()
            .execute()
        )

        if not response.data or not response.data.get("access_token_encrypted"):
            logger.info("No Upstox connection found for user %s", user_id)
            return None

        encrypted_blob = response.data["access_token_encrypted"]
        return decrypt_token(encrypted_blob)
    except Exception as e:
        logger.error("Failed to retrieve Upstox token for user %s: %s", user_id, str(e))
        return None
