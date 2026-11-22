"""Unit-тесты VHS-Viewer: нормализация URL, безопасный редирект, настройка, заголовки."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import viewer  # noqa: E402


def test_secret_key_is_not_default():
    assert viewer.app.secret_key
    assert len(viewer.app.secret_key) >= 32
    assert viewer.app.secret_key != "vhs-viewer-secret-change-me"


def test_default_config_has_no_default_users():
    assert viewer.DEFAULT_CONFIG["security"]["users"] == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ""),
        ("192.168.1.10:8200", "http://192.168.1.10:8200/"),
        ("http://host", "http://host/"),
        ("https://host/", "https://host/"),
    ],
)
def test_normalize_url(raw, expected):
    assert viewer._normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ""),
        ("host:8200", "http://host:8200"),
        ("https://host///", "https://host"),
    ],
)
def test_normalize_server_url(raw, expected):
    assert viewer._normalize_server_url(raw) == expected


def test_normalize_plate():
    assert viewer._normalize_plate(" а 123 вс ") == "А123ВС"


@pytest.mark.parametrize(
    "target, allowed",
    [
        ("/", True),
        ("/events", True),
        ("//evil.example", False),
        ("http://evil.example", False),
        ("https://evil.example/", False),
        ("events", False),
        (None, False),
    ],
)
def test_safe_next(target, allowed):
    result = viewer._safe_next(target, "/fallback")
    assert (result == target) is allowed


def test_setup_flag(tmp_path, monkeypatch):
    config_path = tmp_path / "viewer_config.yaml"
    monkeypatch.setattr(viewer, "CONFIG_PATH", config_path)
    assert viewer._is_setup_complete() is False
    config_path.write_text("_setup_complete: true\n", encoding="utf-8")
    assert viewer._is_setup_complete() is True


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer, "CONFIG_PATH", tmp_path / "viewer_config.yaml")
    viewer.app.config.update(TESTING=True)
    return viewer.app.test_client()


def test_setup_page_available(client):
    response = client.get("/setup")
    assert response.status_code == 200
    assert "VHS-Viewer" in response.get_data(as_text=True)


def test_security_headers(client):
    response = client.get("/setup")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "same-origin"


def test_login_redirects_to_setup(client):
    response = client.get("/login")
    assert response.status_code == 302
    assert "/setup" in response.headers["Location"]
import re

import auth as auth_module


def test_password_hash_roundtrip():
    hashed = auth_module.hash_password("StrongPass123")
    assert hashed.startswith(("pbkdf2:", "scrypt:"))
    assert auth_module.verify_password(hashed, "StrongPass123") is True
    assert auth_module.verify_password(hashed, "WrongPass123") is False


def test_legacy_plaintext_password_still_works():
    assert auth_module.verify_password("legacy-pass", "legacy-pass") is True
    assert auth_module.verify_password("legacy-pass", "other") is False


def _csrf_from(html: str) -> str:
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert match, "csrf token not found"
    return match.group(1)


def test_setup_post_without_csrf_is_rejected(client):
    response = client.post("/setup", data={"streamer_url": "http://x", "username": "a"})
    assert response.status_code == 403


def test_setup_post_with_csrf_reaches_form(client):
    page = client.get("/setup").get_data(as_text=True)
    token = _csrf_from(page)
    response = client.post(
        "/setup",
        data={"_csrf": token, "streamer_url": "", "username": ""},
    )
    assert response.status_code == 200
    assert "URL Streamer обязателен" in response.get_data(as_text=True)


def test_api_post_without_csrf_is_rejected(client):
    response = client.post("/api/security/config", json={})
    assert response.status_code == 403

