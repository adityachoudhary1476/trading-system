"""Tests for the backend services."""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch, MagicMock


class TestBrokerService:
    """Test broker token decryption."""

    def test_decrypt_token_valid_format(self):
        """Test decryption with valid token format."""
        from services.broker import decrypt_token
        import base64
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        # Set up test environment
        os.environ["UPSTOX_TOKEN_ENCRYPTION_KEY"] = "test_secret_key"
        from config import get_settings
        get_settings.cache_clear()

        # Create a test encryption
        secret = "test_secret_key"
        salt = b"finova-upstox-token-v1"
        kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
        key = kdf.derive(secret.encode())

        # Encrypt a test token
        aesgcm = AESGCM(key)
        iv = b"0" * 16  # Test IV
        # AES-GCM: encrypt returns ciphertext + tag
        ciphertext_with_tag = aesgcm.encrypt(iv, b"test_access_token", None)
        # In our format, tag is appended to ciphertext
        ciphertext = ciphertext_with_tag[:-16]
        tag = ciphertext_with_tag[-16:]

        blob = f"{base64.b64encode(iv).decode()}:{base64.b64encode(tag).decode()}:{base64.b64encode(ciphertext).decode()}"

        # Verify the format is correct
        parts = blob.split(":")
        assert len(parts) == 3

        # Verify decryption works
        decrypted = decrypt_token(blob)
        assert decrypted == "test_access_token"

    def test_decrypt_token_malformed(self):
        """Test decryption with malformed token."""
        import os
        os.environ["UPSTOX_TOKEN_ENCRYPTION_KEY"] = "test_secret_key"
        from config import get_settings
        get_settings.cache_clear()

        from services.broker import decrypt_token

        with pytest.raises(ValueError, match="Malformed"):
            decrypt_token("invalid_blob")


class TestMarketDataService:
    """Test market data service."""

    def test_to_upstox_symbol(self):
        """Test symbol conversion."""
        from services.market_data import to_upstox_symbol

        assert to_upstox_symbol("NSE:SBIN") == "NSE_EQ|INE062A01020"
        assert to_upstox_symbol("NSE:RELIANCE") == "NSE_EQ|INE002A01018"
        assert to_upstox_symbol("NSE:INFY") == "NSE_EQ|INE009A01021"
        assert to_upstox_symbol("NSE:NIFTY50") == "NSE_INDEX|Nifty 50"
        assert to_upstox_symbol("NSE:BANKNIFTY") == "NSE_INDEX|Nifty Bank"

    def test_to_upstox_symbol_invalid(self):
        """Test invalid symbol format."""
        from services.market_data import to_upstox_symbol

        with pytest.raises(ValueError):
            to_upstox_symbol("INVALID")
