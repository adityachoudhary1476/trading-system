"""Tests for local Supabase JWT verification (backend/auth.py).

These tests generate keys and tokens in-process. No real Supabase
project, real JWT, real signing key, or real secret is referenced.
"""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import auth
from auth import AuthenticatedUser, get_current_user


# ---------------------------------------------------------------------------
# Test fixtures: a tiny FastAPI app exposing one protected endpoint.
# ---------------------------------------------------------------------------

_ISSUER = "https://test.supabase.co"
_AUDIENCE = "authenticated"


def _protected_app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    async def protected(user: AuthenticatedUser = Depends(get_current_user)):
        return {"user_id": user.user_id, "email": user.email}

    return app


@pytest.fixture
def client():
    return TestClient(_protected_app())


# ---------------------------------------------------------------------------
# Key + token helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, public_pem


@pytest.fixture(scope="module")
def rsa_public_jwk(rsa_keypair):
    _, public_pem = rsa_keypair
    public_key = serialization.load_pem_public_key(public_pem)
    numbers = public_key.public_numbers()
    import base64

    def _b64u(n: int) -> str:
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return {
        "kty": "RSA",
        "kid": "test-rsa-key-1",
        "use": "sig",
        "alg": "RS256",
        "n": _b64u(numbers.n),
        "e": _b64u(numbers.e),
    }


@pytest.fixture(scope="module")
def ec_keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, public_pem


@pytest.fixture(scope="module")
def ec_public_jwk(ec_keypair):
    _, public_pem = ec_keypair
    public_key = serialization.load_pem_public_key(public_pem)
    numbers = public_key.public_numbers()
    import base64

    def _b64u(n: int) -> str:
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return {
        "kty": "EC",
        "kid": "test-ec-key-1",
        "use": "sig",
        "alg": "ES256",
        "crv": "P-256",
        "x": _b64u(numbers.x),
        "y": _b64u(numbers.y),
    }


@pytest.fixture
def settings_with_url(monkeypatch):
    """Force ``get_settings()`` to return a known Supabase URL."""
    from config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SUPABASE_URL", _ISSUER)
    yield
    get_settings.cache_clear()


def _now() -> int:
    return int(time.time())


def _base_claims(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    claims = {
        "iss": _ISSUER + "/auth/v1",
        "aud": _AUDIENCE,
        "sub": "user-1234",
        "email": "user@example.com",
        "iat": _now(),
        "exp": _now() + 3600,
        "role": "authenticated",
    }
    if extra:
        claims.update(extra)
    return claims


# ---------------------------------------------------------------------------
# HS256 (symmetric) tests
# ---------------------------------------------------------------------------


SECRET = "test-supabase-jwt-secret-do-not-use-in-production-32+chars"


class TestHs256Path:
    def test_valid_hs256_token_is_accepted(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        token = jwt.encode(_base_claims(), SECRET, algorithm="HS256",
                           headers={"kid": "symmetric-1"})

        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "user-1234"
        assert body["email"] == "user@example.com"
        get_settings.cache_clear()

    def test_expired_token_is_rejected(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        claims = _base_claims(
            {"exp": _now() - 3600, "iat": _now() - 7200}
        )
        token = jwt.encode(claims, SECRET, algorithm="HS256")

        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or expired authentication token"
        get_settings.cache_clear()

    def test_invalid_signature_is_rejected(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        token = jwt.encode(_base_claims(), "wrong-secret", algorithm="HS256")

        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        get_settings.cache_clear()

    def test_wrong_issuer_is_rejected(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        claims = _base_claims({"iss": "https://evil.example.com/auth/v1"})
        token = jwt.encode(claims, SECRET, algorithm="HS256")

        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        get_settings.cache_clear()

    def test_wrong_audience_is_rejected(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        claims = _base_claims({"aud": "anon"})
        token = jwt.encode(claims, SECRET, algorithm="HS256")

        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        get_settings.cache_clear()

    def test_missing_subject_is_rejected(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        claims = _base_claims()
        claims.pop("sub")
        token = jwt.encode(claims, SECRET, algorithm="HS256")

        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        get_settings.cache_clear()

    def test_alg_none_is_rejected(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        # Construct an unsigned token with alg=none.
        header = {"alg": "none", "typ": "JWT"}
        import base64
        import json

        def _b64(d: dict) -> str:
            return base64.urlsafe_b64encode(
                json.dumps(d, separators=(",", ":")).encode()
            ).rstrip(b"=").decode()

        token = f"{_b64(header)}.{_b64(_base_claims())}."

        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        get_settings.cache_clear()

    def test_unsupported_algorithm_is_rejected(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        # HS512 is not in our allow-list.
        token = jwt.encode(_base_claims(), SECRET, algorithm="HS512")

        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        get_settings.cache_clear()

    def test_malformed_token_is_rejected(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        response = client.get(
            "/protected", headers={"Authorization": "Bearer not-a-jwt"}
        )
        assert response.status_code == 401
        get_settings.cache_clear()

    def test_missing_authorization_header_is_rejected(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        response = client.get("/protected")
        assert response.status_code == 401
        assert response.json()["detail"] == "Authentication required"
        get_settings.cache_clear()

    def test_empty_bearer_token_is_rejected(self, client, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        response = client.get(
            "/protected", headers={"Authorization": "Bearer "}
        )
        # FastAPI's HTTPBearer rejects an empty bearer before our
        # dependency even runs, so the response is "Authentication
        # required" (same code path as a missing header).
        assert response.status_code == 401
        assert response.headers.get("www-authenticate") == "Bearer"
        get_settings.cache_clear()

    def test_www_authenticate_header_is_always_set_on_401(
        self, client, monkeypatch
    ):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        response = client.get("/protected")
        assert response.status_code == 401
        assert response.headers.get("www-authenticate") == "Bearer"
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# RS256 / ES256 (asymmetric) tests — JWKS path
# ---------------------------------------------------------------------------


class _StubPyJWKClient:
    """A minimal PyJWKClient substitute used to avoid network I/O.

    Mirrors the part of the PyJWKClient API that ``auth.py`` exercises
    (``get_signing_key_from_jwt`` returning an object with ``.key``).
    """

    def __init__(self, jwks: dict, keys_by_kid: dict[str, Any]):
        self.jwks = jwks
        self.keys_by_kid = keys_by_kid
        self.fetch_count = 0

    def get_signing_key_from_jwt(self, token: str):
        self.fetch_count += 1
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if kid is None or kid not in self.keys_by_kid:
            raise jwt.PyJWKClientError(f"unknown kid {kid!r}")
        return self.keys_by_kid[kid]


def _patch_jwks(jwks: dict, keys_by_kid: dict[str, Any]):
    """Patch ``auth._get_jwks_client`` to return a stub."""
    stub = _StubPyJWKClient(jwks, keys_by_kid)
    return patch.object(auth, "_get_jwks_client", return_value=stub), stub


def _rsa_jwk_key(jwk: dict, public_pem: bytes):
    from jwt import PyJWK

    return PyJWK.from_dict(jwk)


def _ec_jwk_key(jwk: dict, public_pem: bytes):
    from jwt import PyJWK

    return PyJWK.from_dict(jwk)


class TestAsymmetricRs256Path:
    def test_valid_rs256_token_is_accepted(
        self, client, monkeypatch, rsa_keypair, rsa_public_jwk
    ):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)

        private_pem, _ = rsa_keypair
        token = jwt.encode(
            _base_claims(),
            private_pem,
            algorithm="RS256",
            headers={"kid": "test-rsa-key-1"},
        )

        keys_by_kid = {
            "test-rsa-key-1": _rsa_jwk_key(
                rsa_public_jwk, rsa_keypair[1]
            )
        }
        jwks = {"keys": [rsa_public_jwk]}
        patcher, _ = _patch_jwks(jwks, keys_by_kid)
        with patcher:
            response = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "user-1234"
        assert body["email"] == "user@example.com"
        get_settings.cache_clear()

    def test_expired_rs256_token_is_rejected(
        self, client, monkeypatch, rsa_keypair, rsa_public_jwk
    ):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        private_pem, _ = rsa_keypair
        claims = _base_claims({"exp": _now() - 3600, "iat": _now() - 7200})
        token = jwt.encode(
            claims, private_pem, algorithm="RS256",
            headers={"kid": "test-rsa-key-1"},
        )
        keys_by_kid = {
            "test-rsa-key-1": _rsa_jwk_key(rsa_public_jwk, rsa_keypair[1])
        }
        patcher, _ = _patch_jwks({"keys": [rsa_public_jwk]}, keys_by_kid)
        with patcher:
            response = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 401
        get_settings.cache_clear()

    def test_rs256_signed_with_wrong_key_is_rejected(
        self, client, monkeypatch, rsa_keypair, rsa_public_jwk
    ):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        # Sign with a brand-new key, but tell the verifier the kid maps
        # to a different public key.
        other_private = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        other_pem = other_private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        token = jwt.encode(
            _base_claims(), other_pem, algorithm="RS256",
            headers={"kid": "test-rsa-key-1"},
        )
        # Verifier uses rsa_keypair's public key, not the signer's.
        _, public_pem = rsa_keypair
        keys_by_kid = {
            "test-rsa-key-1": _rsa_jwk_key(rsa_public_jwk, public_pem)
        }
        patcher, _ = _patch_jwks({"keys": [rsa_public_jwk]}, keys_by_kid)
        with patcher:
            response = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 401
        get_settings.cache_clear()

    def test_rs256_wrong_audience_is_rejected(
        self, client, monkeypatch, rsa_keypair, rsa_public_jwk
    ):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        private_pem, _ = rsa_keypair
        token = jwt.encode(
            _base_claims({"aud": "anon"}), private_pem, algorithm="RS256",
            headers={"kid": "test-rsa-key-1"},
        )
        keys_by_kid = {
            "test-rsa-key-1": _rsa_jwk_key(rsa_public_jwk, rsa_keypair[1])
        }
        patcher, _ = _patch_jwks({"keys": [rsa_public_jwk]}, keys_by_kid)
        with patcher:
            response = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 401
        get_settings.cache_clear()

    def test_rs256_wrong_issuer_is_rejected(
        self, client, monkeypatch, rsa_keypair, rsa_public_jwk
    ):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        private_pem, _ = rsa_keypair
        token = jwt.encode(
            _base_claims({"iss": "https://evil.example.com/auth/v1"}),
            private_pem, algorithm="RS256",
            headers={"kid": "test-rsa-key-1"},
        )
        keys_by_kid = {
            "test-rsa-key-1": _rsa_jwk_key(rsa_public_jwk, rsa_keypair[1])
        }
        patcher, _ = _patch_jwks({"keys": [rsa_public_jwk]}, keys_by_kid)
        with patcher:
            response = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 401
        get_settings.cache_clear()


class TestAsymmetricEs256Path:
    def test_valid_es256_token_is_accepted(
        self, client, monkeypatch, ec_keypair, ec_public_jwk
    ):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        private_pem, _ = ec_keypair
        token = jwt.encode(
            _base_claims(), private_pem, algorithm="ES256",
            headers={"kid": "test-ec-key-1"},
        )
        keys_by_kid = {
            "test-ec-key-1": _ec_jwk_key(ec_public_jwk, ec_keypair[1])
        }
        patcher, _ = _patch_jwks({"keys": [ec_public_jwk]}, keys_by_kid)
        with patcher:
            response = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 200
        get_settings.cache_clear()

    def test_es256_expired_is_rejected(
        self, client, monkeypatch, ec_keypair, ec_public_jwk
    ):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        private_pem, _ = ec_keypair
        claims = _base_claims({"exp": _now() - 3600, "iat": _now() - 7200})
        token = jwt.encode(
            claims, private_pem, algorithm="ES256",
            headers={"kid": "test-ec-key-1"},
        )
        keys_by_kid = {
            "test-ec-key-1": _ec_jwk_key(ec_public_jwk, ec_keypair[1])
        }
        patcher, _ = _patch_jwks({"keys": [ec_public_jwk]}, keys_by_kid)
        with patcher:
            response = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 401
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# JWKS caching + key-rotation behaviour
# ---------------------------------------------------------------------------


class TestJwksCaching:
    def test_jwks_client_is_cached(self, monkeypatch, rsa_public_jwk):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)

        # Force the cache to be empty.
        with auth._JWKS_CLIENTS_LOCK:
            auth._JWKS_CLIENTS.clear()

        calls = {"n": 0}

        def fake_fetch(_self, url):
            calls["n"] += 1
            return {"keys": [rsa_public_jwk]}

        with patch.object(
            auth.PyJWKClient, "fetch_data", new=fake_fetch
        ):
            client1 = auth._get_jwks_client(_ISSUER)
            client2 = auth._get_jwks_client(_ISSUER)
            assert client1 is client2
            # We never call fetch_data directly — PyJWKClient lazily
            # fetches. The important invariant is that the same client
            # object is returned on repeated calls.
        get_settings.cache_clear()

    def test_key_rotation_is_picked_up(
        self, client, monkeypatch, rsa_keypair
    ):
        """When the project's ``kid`` rotates, the verifier should
        look up the new key by ``kid`` and accept tokens signed with
        it. Tokens whose ``kid`` is no longer in the JWKS must be
        rejected."""
        from config import get_settings
        from jwt import PyJWK
        import base64

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)

        def _b64u(n: int) -> str:
            b = n.to_bytes((n.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

        def _to_jwk(kid: str, public_key) -> dict:
            numbers = public_key.public_numbers()
            return {
                "kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
                "n": _b64u(numbers.n), "e": _b64u(numbers.e),
            }

        # First signing key (the "old" key).
        priv_old = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        priv_old_pem = priv_old.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        jwk_old = _to_jwk("old-kid", priv_old.public_key())

        # Second signing key (the "new" key, rotated in).
        priv_new = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        priv_new_pem = priv_new.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        jwk_new = _to_jwk("new-kid", priv_new.public_key())

        # The JWKS document only contains the NEW key. A token signed
        # with the OLD key (kid=old-kid) must be rejected because the
        # verifier cannot find that key.
        jwk_new_key = PyJWK.from_dict(jwk_new)
        keys_by_kid = {"new-kid": jwk_new_key}
        patcher, _ = _patch_jwks({"keys": [jwk_new]}, keys_by_kid)
        with patcher:
            # New key, new kid: accepted.
            new_token = jwt.encode(
                _base_claims(), priv_new_pem, algorithm="RS256",
                headers={"kid": "new-kid"},
            )
            response = client.get(
                "/protected", headers={"Authorization": f"Bearer {new_token}"}
            )
            assert response.status_code == 200

            # Old key, old kid: kid not in JWKS -> 401.
            old_token = jwt.encode(
                _base_claims(), priv_old_pem, algorithm="RS256",
                headers={"kid": "old-kid"},
            )
            response = client.get(
                "/protected",
                headers={"Authorization": f"Bearer {old_token}"},
            )
            assert response.status_code == 401
        get_settings.cache_clear()

    def test_jwks_endpoint_failure_falls_back_to_symmetric(
        self, client, monkeypatch, rsa_keypair, rsa_public_jwk
    ):
        """If the JWKS endpoint is unreachable but a JWT secret is
        configured, a HS256 token signed with the secret should still
        be accepted."""
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        token = jwt.encode(_base_claims(), SECRET, algorithm="HS256",
                           headers={"kid": "symmetric-1"})

        with patch.object(
            auth, "_get_jwks_client",
            side_effect=Exception("JWKS endpoint down"),
        ):
            response = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 200
        get_settings.cache_clear()

    def test_jwks_endpoint_failure_without_secret_is_rejected(
        self, client, monkeypatch
    ):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        # No SUPABASE_JWT_SECRET set.
        token = jwt.encode(_base_claims(), SECRET, algorithm="HS256",
                           headers={"kid": "symmetric-1"})

        with patch.object(
            auth, "_get_jwks_client",
            side_effect=Exception("JWKS endpoint down"),
        ):
            response = client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 401
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Algorithm allow-list + unit tests on the claims-to-user helper
# ---------------------------------------------------------------------------


class TestUnitHelpers:
    def test_supported_algorithms_list(self):
        assert auth.SUPPORTED_ALGORITHMS == ("ES256", "RS256", "HS256")
        # ``none`` must not be accepted.
        assert "none" not in auth.SUPPORTED_ALGORITHMS

    def test_issuer_is_built_from_supabase_url(self, monkeypatch):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co/")
        s = get_settings()
        assert auth._issuer_for(s.supabase_url) == "https://x.supabase.co/auth/v1"
        get_settings.cache_clear()

    def test_jwks_url_is_built_from_supabase_url(self):
        assert (
            auth._jwks_url_for("https://x.supabase.co/")
            == "https://x.supabase.co/auth/v1/.well-known/jwks.json"
        )

    def test_claims_to_user_rejects_non_string_sub(self):
        assert auth._claims_to_user({"sub": 123}) is None
        assert auth._claims_to_user({"sub": ""}) is None
        assert auth._claims_to_user({}) is None

    def test_claims_to_user_extracts_email(self):
        u = auth._claims_to_user({"sub": "u-1", "email": "a@b.c"})
        assert u is not None
        assert u.user_id == "u-1"
        assert u.email == "a@b.c"

    def test_claims_to_user_ignores_non_string_email(self):
        u = auth._claims_to_user({"sub": "u-1", "email": 7})
        assert u is not None
        assert u.email is None


# ---------------------------------------------------------------------------
# End-to-end smoke against the real FastAPI app + analysis/signals routes
# ---------------------------------------------------------------------------


class TestRoutesIntegration:
    def test_analysis_endpoint_rejects_missing_token(self, monkeypatch):
        from main import app
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        client = TestClient(app)
        try:
            response = client.get("/api/market/analysis?symbol=NSE:SBIN")
        finally:
            get_settings.cache_clear()
        assert response.status_code == 401
        assert response.headers.get("www-authenticate") == "Bearer"

    def test_signals_endpoint_rejects_missing_token(self, monkeypatch):
        from main import app
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        client = TestClient(app)
        try:
            response = client.get("/api/market/signals")
        finally:
            get_settings.cache_clear()
        assert response.status_code == 401
        assert response.headers.get("www-authenticate") == "Bearer"

    def test_analysis_endpoint_accepts_valid_token(
        self, monkeypatch, rsa_keypair, rsa_public_jwk
    ):
        from main import app
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        private_pem, _ = rsa_keypair
        token = jwt.encode(
            _base_claims(), private_pem, algorithm="RS256",
            headers={"kid": "test-rsa-key-1"},
        )
        keys_by_kid = {
            "test-rsa-key-1": _rsa_jwk_key(rsa_public_jwk, rsa_keypair[1])
        }
        patcher, _ = _patch_jwks({"keys": [rsa_public_jwk]}, keys_by_kid)
        client = TestClient(app)
        try:
            with patcher:
                with patch("routes.analysis.broker.get_upstox_access_token",
                           return_value=None):
                    response = client.get(
                        "/api/market/analysis?symbol=NSE:SBIN",
                        headers={"Authorization": f"Bearer {token}"},
                    )
        finally:
            get_settings.cache_clear()
        # Auth passed -> we hit the 403 "Upstox not connected" path.
        assert response.status_code == 403
        assert "Upstox" in response.json()["detail"]

    def test_signals_endpoint_accepts_valid_token(
        self, monkeypatch, rsa_keypair, rsa_public_jwk
    ):
        from main import app
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        private_pem, _ = rsa_keypair
        token = jwt.encode(
            _base_claims(), private_pem, algorithm="RS256",
            headers={"kid": "test-rsa-key-1"},
        )
        keys_by_kid = {
            "test-rsa-key-1": _rsa_jwk_key(rsa_public_jwk, rsa_keypair[1])
        }
        patcher, _ = _patch_jwks({"keys": [rsa_public_jwk]}, keys_by_kid)
        client = TestClient(app)
        try:
            with patcher:
                with patch("routes.signals.broker.get_upstox_access_token",
                           return_value=None):
                    response = client.get(
                        "/api/market/signals",
                        headers={"Authorization": f"Bearer {token}"},
                    )
        finally:
            get_settings.cache_clear()
        assert response.status_code == 403
        assert "Upstox" in response.json()["detail"]


# ---------------------------------------------------------------------------
# No-secrets-leak guard
# ---------------------------------------------------------------------------


class TestNoSecretLogging:
    def test_no_jwt_or_secret_in_logs(self, caplog, monkeypatch, rsa_keypair,
                                       rsa_public_jwk, client):
        from config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_URL", _ISSUER)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)

        private_pem, _ = rsa_keypair
        token = jwt.encode(
            _base_claims(), private_pem, algorithm="RS256",
            headers={"kid": "test-rsa-key-1"},
        )

        keys_by_kid = {
            "test-rsa-key-1": _rsa_jwk_key(rsa_public_jwk, rsa_keypair[1])
        }
        patcher, _ = _patch_jwks({"keys": [rsa_public_jwk]}, keys_by_kid)
        caplog.set_level("INFO", logger="auth")
        with patcher:
            # Both a successful call and a failure call so log handlers
            # get exercised on both paths.
            client.get(
                "/protected", headers={"Authorization": f"Bearer {token}"}
            )
            client.get(
                "/protected", headers={"Authorization": "Bearer garbage"}
            )
        joined = caplog.text
        assert token not in joined
        assert SECRET not in joined
        assert "eyJ" not in joined  # no JWT-shaped substrings
        get_settings.cache_clear()
