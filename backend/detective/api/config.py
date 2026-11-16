"""Explicit configuration: no default database credentials or signing secret."""

import ipaddress
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    token_hash_pepper: str = field(repr=False)
    allowed_origins: tuple[str, ...]
    case_storage_path: Path
    cookie_secure: bool = True
    database_schema: str = "public"
    session_days: int = 30
    environment: str = "development"
    forwarded_allow_ips: tuple[str, ...] = ()
    http_rate_limit_enabled: bool | None = None

    @property
    def rate_limit_enabled(self) -> bool:
        return (
            self.environment == "production"
            if self.http_rate_limit_enabled is None
            else self.http_rate_limit_enabled
        )

    def __post_init__(self) -> None:
        if self.environment not in ("development", "production"):
            raise ValueError("APP_ENV must be development or production")
        for peer in self.forwarded_allow_ips:
            try:
                ip = ipaddress.ip_address(peer)
            except ValueError:
                raise ValueError(
                    "FORWARDED_ALLOW_IPS must contain exact proxy IP addresses"
                ) from None
            if ip.is_unspecified or ip.is_multicast:
                raise ValueError("Invalid trusted proxy address")
        if self.environment == "production":
            if (
                not self.cookie_secure
                or not self.rate_limit_enabled
                or not self.forwarded_allow_ips
            ):
                raise ValueError(
                    "Production requires Secure cookies, rate limits and explicit proxy trust"
                )
            if (
                not self.case_storage_path.is_absolute()
                or ".." in self.case_storage_path.parts
                or self.case_storage_path == Path("/")
                or any(
                    self.case_storage_path == root or root in self.case_storage_path.parents
                    for root in (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))
                )
            ):
                raise ValueError("Production requires an absolute persistent CASE_STORAGE_PATH")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("PostgreSQL with asyncpg is required")
        if len(self.token_hash_pepper.encode()) < 32:
            raise ValueError("TOKEN_HASH_PEPPER must contain at least 32 bytes")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.database_schema):
            raise ValueError("Invalid DATABASE_SCHEMA")
        if not self.allowed_origins or not 1 <= self.session_days <= 365:
            raise ValueError("Invalid origins/session lifetime")
        for origin in self.allowed_origins:
            try:
                parsed = urlsplit(origin)
                port = parsed.port
                if port == 0:
                    raise ValueError("Invalid origin port")
            except ValueError:
                raise ValueError("Invalid ALLOWED_ORIGINS address/port") from None
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or "*" in origin
                or any(char.isspace() for char in origin)
                or (
                    parsed.path
                    or parsed.query
                    or parsed.fragment
                    or parsed.username
                    or parsed.password
                )
            ):
                raise ValueError("ALLOWED_ORIGINS must contain exact origins")
            hostname = parsed.hostname or ""
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                labels = hostname.split(".")
                if (
                    len(hostname) > 253
                    or not all(
                        re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                        for label in labels
                    )
                    or all(label.isdecimal() for label in labels)
                ):
                    raise ValueError("Invalid ALLOWED_ORIGINS hostname") from None
            if not self.cookie_secure and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Insecure cookies are allowed only for loopback development")
            if self.cookie_secure and parsed.scheme != "https":
                raise ValueError("Secure cookie mode requires HTTPS origins")

    @property
    def cookie_name(self) -> str:
        return "__Host-detective_session" if self.cookie_secure else "detective_session"

    @classmethod
    def from_env(cls) -> "Settings":
        secure = os.environ.get("SESSION_COOKIE_SECURE", "true")
        if secure not in ("true", "false"):
            raise ValueError("SESSION_COOKIE_SECURE must be true or false")
        rate = os.environ.get("HTTP_RATE_LIMIT_ENABLED")
        if rate not in (None, "true", "false"):
            raise ValueError("HTTP_RATE_LIMIT_ENABLED must be true or false")
        return cls(
            database_url=secret_value("DATABASE_URL"),
            token_hash_pepper=secret_value("TOKEN_HASH_PEPPER"),
            allowed_origins=tuple(os.environ["ALLOWED_ORIGINS"].split(",")),
            case_storage_path=Path(os.environ.get("CASE_STORAGE_PATH", ".local/cases")),
            cookie_secure=secure == "true",
            database_schema=os.environ.get("DATABASE_SCHEMA", "public"),
            environment=os.environ.get("APP_ENV", "development"),
            forwarded_allow_ips=tuple(
                filter(None, os.environ.get("FORWARDED_ALLOW_IPS", "").split(","))
            ),
            http_rate_limit_enabled=None if rate is None else rate == "true",
        )


def secret_value(name: str) -> str:
    """Read an env secret or mounted file without reflecting its contents in errors."""
    direct, file = os.environ.get(name), os.environ.get(name + "_FILE")
    if direct is not None and file is not None:
        raise ValueError(f"Set only {name} or {name}_FILE")
    if file is None:
        if direct is None:
            raise ValueError(f"Missing {name} or {name}_FILE")
        return direct
    try:
        descriptor = os.open(
            file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Not a regular secret file")
            content = stream.read(16385)
        if len(content) > 16384:
            raise ValueError("Secret file too large")
        result = content.decode("utf-8").rstrip("\r\n")
        if not result or "\n" in result or "\r" in result:
            raise ValueError("Invalid secret file")
        return result
    except (OSError, ValueError, UnicodeError):
        raise ValueError(f"Unable to read valid {name}_FILE") from None
