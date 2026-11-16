"""Fail-closed production config and bounded HTTP budgets without external services."""

from dataclasses import replace
from pathlib import Path

import pytest
from detective.api.config import Settings
from detective.api.rate_limit import HTTPRateLimiter


def production():
    return Settings(
        "postgresql+asyncpg://user:disposable@db/detective",
        "x" * 64,
        ("https://detective.example",),
        Path("/data/cases"),
        environment="production",
        forwarded_allow_ips=("172.30.0.2",),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"cookie_secure": False},
        {"http_rate_limit_enabled": False},
        {"allowed_origins": ("http://detective.example",)},
        {"allowed_origins": ("https://*.example",)},
        {"allowed_origins": ("https://detective.example:invalid",)},
        {"allowed_origins": ("https://detective.example:99999",)},
        {"allowed_origins": ("https://detective.example:0",)},
        {"allowed_origins": ("https://bad_host.example",)},
        {"allowed_origins": ("https://%secret.example",)},
        {"allowed_origins": ("https://user:secret@detective.example",)},
        {"allowed_origins": ("https://detective.example/private",)},
        {"allowed_origins": ("https://detective.example ",)},
        {"forwarded_allow_ips": ()},
        {"forwarded_allow_ips": ("*",)},
        {"forwarded_allow_ips": ("172.30.0.0/24",)},
        {"forwarded_allow_ips": ("0.0.0.0",)},
        {"case_storage_path": Path("relative")},
        {"case_storage_path": Path("/tmp/cases")},
        {"case_storage_path": Path("/")},
        {"environment": "prod-typo"},
    ],
)
def test_production_rejects_unsafe_config(changes):
    with pytest.raises(ValueError):
        replace(production(), **changes)


def test_production_defaults_and_development_compatibility():
    prod = production()
    assert prod.rate_limit_enabled and prod.cookie_name == "__Host-detective_session"
    dev = replace(
        prod,
        environment="development",
        cookie_secure=False,
        allowed_origins=("http://127.0.0.1:5173",),
        forwarded_allow_ips=(),
        case_storage_path=Path(".local/cases"),
    )
    assert not dev.rate_limit_enabled and dev.cookie_name == "detective_session"
    assert "disposable" not in repr(prod) and "x" * 64 not in repr(prod)


def test_secret_files_are_bounded_and_never_reflected(tmp_path, monkeypatch):
    for name in (
        "DATABASE_URL",
        "TOKEN_HASH_PEPPER",
        "DATABASE_URL_FILE",
        "TOKEN_HASH_PEPPER_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://detective.example")
    monkeypatch.setenv("CASE_STORAGE_PATH", "/data/cases")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.30.0.2")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    database, pepper = tmp_path / "database", tmp_path / "pepper"
    database.write_text(production().database_url + "\n")
    pepper.write_text("PRIVATE_TEST_SECRET_" * 4 + "\n")
    monkeypatch.setenv("DATABASE_URL_FILE", str(database))
    monkeypatch.setenv("TOKEN_HASH_PEPPER_FILE", str(pepper))
    assert Settings.from_env().token_hash_pepper == "PRIVATE_TEST_SECRET_" * 4
    monkeypatch.setenv("TOKEN_HASH_PEPPER", "other" * 8)
    with pytest.raises(ValueError, match="Set only"):
        Settings.from_env()
    monkeypatch.delenv("TOKEN_HASH_PEPPER")
    for value in ("PRIVATE_TEST_SECRET_" * 1000, "private\nsecret", ""):
        pepper.write_text(value)
        with pytest.raises(ValueError) as error:
            Settings.from_env()
        assert "PRIVATE_TEST_SECRET_" not in str(error.value)
    pepper.unlink()
    pepper.symlink_to(database)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_rate_limits_refill_independently_and_bound_capacity():
    limits = HTTPRateLimiter(max_keys=4)
    for _ in range(5):
        assert limits.check("peer", "POST", "/api/v1/rooms", now=0) == 0
    assert limits.check("peer", "POST", "/api/v1/rooms", now=0) == 6
    assert limits.check("peer", "POST", "/api/v1/rooms", now=6) == 0
    assert limits.check("other", "POST", "/api/v1/rooms", now=6) == 0
    assert limits.check("third", "GET", "/api/v1/cases", now=6) == 60
    assert len(limits.buckets) == 4
    assert limits.check("third", "GET", "/api/v1/cases", now=4000) == 0
    assert len(limits.buckets) == 1


def test_import_and_bootstrap_budgets_do_not_block_ordinary_reads():
    limits = HTTPRateLimiter()
    for _ in range(3):
        assert limits.check("peer", "POST", "/api/v1/rooms/roomcode/cases/import", now=0) == 0
    assert limits.check("peer", "POST", "/api/v1/rooms/roomcode/cases/import", now=0) == 300
    assert limits.check("peer", "GET", "/api/v1/cases", now=0) == 0
    for _ in range(10):
        assert limits.check("peer", "POST", "/api/v1/sessions", now=0) == 0
    assert limits.check("peer", "POST", "/api/v1/sessions", now=0) == 2


def test_general_budget_limits_snapshot_flood_and_refills():
    limits = HTTPRateLimiter()
    for _ in range(240):
        assert limits.check("peer", "GET", "/api/v1/rooms/roomcode/snapshot", now=0) == 0
    assert limits.check("peer", "GET", "/api/v1/rooms/roomcode/snapshot", now=0) == 1
    assert limits.check("other", "GET", "/api/v1/cases", now=0) == 0
    assert limits.check("peer", "GET", "/api/v1/cases", now=0.1) == 0


def test_shared_nat_allows_eight_players_following_board_updates():
    limits = HTTPRateLimiter()
    # One room update per 250ms poll, one personal snapshot per participant.
    # A shared IP must not deny an ordinary group while still bounding floods.
    for tick in range(120):
        for _ in range(8):
            assert (
                limits.check("shared-nat", "GET", "/api/v1/rooms/roomcode/snapshot", now=tick / 4)
                == 0
            )
