"""Explicit single-project Docker operations; no shell or default environment file."""

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = ("database_url", "postgres_password", "token_hash_pepper")
VOLUMES = ("database", "cases", "caddy_data", "caddy_config")
ARCHIVES = {
    "cases": "cases.tar",
    "caddy_data": "caddy_data.tar",
    "caddy_config": "caddy_config.tar",
}
PUBLIC_KEYS = {
    "PUBLIC_DOMAIN",
    "ACME_EMAIL",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "SECRETS_DIR",
    "BIND_ADDRESS",
    "HTTP_PORT",
    "HTTPS_PORT",
    "NETWORK_SUBNET",
    "NETWORK_DYNAMIC_RANGE",
    "PROXY_IP",
    "IMAGE_TAG",
}


def read_env(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in PUBLIC_KEYS or any(c in value for c in "\r\n$`\"'"):
            raise ValueError("Invalid public environment file")
        result[key] = value
    for key in ("POSTGRES_DB", "POSTGRES_USER"):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", result.get(key, "")):
            raise ValueError("Invalid database identifier")
    return result


def private_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    path.chmod(mode)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_tar(path: Path) -> None:
    """Never accept traversal, devices or links; backups are still untrusted input."""
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            target = PurePosixPath(member.name)
            if (
                target.is_absolute()
                or ".." in target.parts
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Unsafe backup archive")


class Operations:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.env_file = args.env_file.resolve()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,55}", args.project):
            raise ValueError("Explicit valid project name required")
        if args.test and not args.project.startswith("detective-test-"):
            raise ValueError("Test project must start with detective-test-")
        self.project = args.project
        self.env = read_env(self.env_file)
        if args.test and self.env.get("BIND_ADDRESS") != "127.0.0.1":
            raise ValueError("Test ports must bind to loopback")
        self.secrets = Path(self.env["SECRETS_DIR"])
        if not self.secrets.is_absolute() or self.secrets.is_symlink():
            raise ValueError("Private secrets directory must be an absolute ordinary path")
        executable = shutil.which("docker")
        if not executable:
            candidate = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
            if not candidate.exists():
                raise RuntimeError("Docker CLI is unavailable")
            executable = str(candidate)
        self.docker = executable
        mode = self.secrets.stat().st_mode & 0o777
        if mode != 0o700 or self.secrets.stat().st_uid != os.geteuid():
            raise ValueError("Secrets directory must be owned by current user and have mode0700")
        self.base = [
            self.docker,
            "compose",
            "--env-file",
            str(self.env_file),
            "-p",
            self.project,
            "-f",
            str(ROOT / "compose.yaml"),
        ]
        if args.test:
            self.base += ["-f", str(ROOT / "compose.test.yaml")]
        self.process_env = {
            key: value for key, value in os.environ.items() if key not in PUBLIC_KEYS
        }
        self.process_env["PATH"] = (
            str(Path(self.docker).parent) + os.pathsep + os.environ.get("PATH", "")
        )

    @contextmanager
    def maintenance_lock(self):
        lock_path = self.env_file.with_suffix(self.env_file.suffix + ".ops.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def run(
        self, command: list[str], *, output=None, input_data=None, input_stream=None, capture=False
    ):
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=self.process_env,
            input=input_data,
            stdin=input_stream,
            stdout=subprocess.PIPE if capture else output,
            stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise RuntimeError("Docker operation failed; private command output withheld")
        return result.stdout

    def compose(self, *command: str, **kwargs):
        return self.run([*self.base, *command], **kwargs)

    def image(self, service: str) -> str:
        return f"detective-{service}:{self.env.get('IMAGE_TAG', 'local')}"

    def volume(self, name: str) -> str:
        value = f"{self.project}_{name}"
        raw = self.run([self.docker, "volume", "inspect", value], capture=True)
        info = json.loads(raw)[0]
        if info.get("Labels", {}).get("com.docker.compose.project") != self.project:
            raise ValueError("Refusing a volume not owned by this Compose project")
        return value

    def healthy_db(self) -> None:
        self.compose("up", "-d", "--wait", "db")
        # Admin is used only for bootstrap/dump/restore. API and Alembic use a
        # separate non-superuser owner, with no ability to create databases/roles.
        url = urlsplit((self.secrets / "database_url").read_text().strip())
        if url.username != "detective_app" or not url.password:
            raise ValueError("DATABASE_URL must use the dedicated detective_app role")
        password = unquote(url.password).replace("'", "''")
        name = self.env["POSTGRES_DB"]
        sql = (
            "SET log_statement='none'; SET log_min_error_statement='panic'; "
            "SET log_min_messages='panic'; "
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='detective_app') "
            "THEN CREATE ROLE detective_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION; END IF; END $$; "
            "ALTER ROLE detective_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            f"PASSWORD '{password}'; "
            f'ALTER DATABASE "{name}" OWNER TO detective_app; '
            "ALTER SCHEMA public OWNER TO detective_app; "
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC; "
            f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC;'
        )
        self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            self.env["POSTGRES_USER"],
            "-d",
            name,
            "-v",
            "ON_ERROR_STOP=1",
            input_data=sql.encode(),
            capture=True,
        )

    def start(self, *, build=True) -> None:
        if build:
            self.compose("build")
        self.compose("stop", "proxy", "api")
        self.healthy_db()
        self.compose("run", "--rm", "--no-deps", "cases-init")
        self.compose("run", "--rm", "migrate")
        self.compose("up", "-d", "--wait", "api", "proxy")

    def archive_volume(self, logical: str, output: Path) -> None:
        image = self.image("api") if logical == "cases" else self.image("proxy")
        volume = self.volume(logical)
        with output.open("xb") as stream:
            output.chmod(0o600)
            command = [
                self.docker,
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{volume}:/source:ro",
                "--entrypoint",
                "tar",
                image,
                "-C",
                "/source",
            ]
            if logical == "cases":
                command += ["--exclude=.upload-*"]
            self.run([*command, "-cf", "-", "."], output=stream)

    def backup(self, target: Path) -> None:
        target = target.resolve()
        if target.exists():
            raise ValueError("Backup destination must not exist")
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=".detective-backup-", dir=target.parent))
        active = (
            self.compose("ps", "--status", "running", "--services", capture=True).decode().split()
        )
        restart = [name for name in ("api", "proxy") if name in active]
        try:
            self.compose("stop", "proxy", "api")
            self.healthy_db()
            dump = staged / "database.dump"
            with dump.open("xb") as stream:
                dump.chmod(0o600)
                self.compose(
                    "exec",
                    "-T",
                    "db",
                    "pg_dump",
                    "-U",
                    self.env["POSTGRES_USER"],
                    "-d",
                    self.env["POSTGRES_DB"],
                    "-Fc",
                    "--no-owner",
                    output=stream,
                )
            for logical, filename in ARCHIVES.items():
                self.archive_volume(logical, staged / filename)
            (staged / "secrets").mkdir(mode=0o700)
            for name in SECRET_NAMES:
                private_write(staged / "secrets" / name, (self.secrets / name).read_bytes())
            private_write(staged / "public.env", self.env_file.read_bytes())
            checksums = {
                str(path.relative_to(staged)): file_hash(path)
                for path in staged.rglob("*")
                if path.is_file()
            }
            manifest = {
                "backup_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "source_project": self.project,
                "schema_head": self.compose(
                    "exec",
                    "-T",
                    "db",
                    "psql",
                    "-U",
                    self.env["POSTGRES_USER"],
                    "-d",
                    self.env["POSTGRES_DB"],
                    "-tAc",
                    "SELECT version_num FROM alembic_version",
                    capture=True,
                )
                .decode()
                .strip(),
                "image_tag": self.env.get("IMAGE_TAG", "local"),
                "images": {
                    name: self.run(
                        [self.docker, "image", "inspect", "--format", "{{.Id}}", self.image(name)],
                        capture=True,
                    )
                    .decode()
                    .strip()
                    for name in ("api", "proxy")
                },
                "sha256": checksums,
            }
            private_write(staged / "manifest.json", json.dumps(manifest, indent=2).encode())
            staged.rename(target)
        finally:
            if staged.exists():
                shutil.rmtree(staged)
            if restart:
                self.compose("up", "-d", "--wait", *restart)

    def verify_backup(self, source: Path) -> None:
        if source.is_symlink():
            raise ValueError("Invalid backup directory")
        manifest = json.loads((source / "manifest.json").read_text())
        required = {
            "database.dump",
            "public.env",
            *ARCHIVES.values(),
            *(f"secrets/{name}" for name in SECRET_NAMES),
        }
        if manifest.get("backup_version") != 1 or set(manifest.get("sha256", {})) != required:
            raise ValueError("Unsupported or incomplete backup")
        for name in required:
            path = source / name
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve().is_relative_to(source)
                or file_hash(path) != manifest["sha256"][name]
            ):
                raise ValueError("Backup checksum mismatch")
        for filename in ARCHIVES.values():
            validate_tar(source / filename)

    def restore(self, source: Path) -> None:
        if source.is_symlink():
            raise ValueError("Invalid backup source")
        source = source.resolve()
        self.verify_backup(source)
        # No down -v, no clean or implicit replacement of existing target volumes.
        volumes = self.run(
            [
                self.docker,
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
            capture=True,
        )
        containers = self.compose("ps", "-aq", capture=True)
        if volumes.strip() or containers.strip():
            raise ValueError("Restore requires a fresh project without containers or volumes")
        password = (source / "secrets/postgres_password").read_text().strip()
        app_url = urlsplit((source / "secrets/database_url").read_text().strip())
        if app_url.username != "detective_app" or not app_url.password:
            raise ValueError("Unsupported backup database role")
        app_password = app_url.password
        replacement = {
            "postgres_password": password,
            "token_hash_pepper": (source / "secrets/token_hash_pepper").read_text().strip(),
            "database_url": f"postgresql+asyncpg://detective_app:{app_password}@db:5432/{self.env['POSTGRES_DB']}",
        }
        for name, value in replacement.items():
            destination = self.secrets / name
            if destination.is_symlink():
                raise ValueError("Invalid secrets destination")
            destination.chmod(0o600)
            destination.write_text(value + "\n")
            destination.chmod(0o600 if name == "postgres_password" else 0o444)
        self.healthy_db()
        with (source / "database.dump").open("rb") as stream:
            # pg_restore interprets trusted backup SQL: accept backups only from your
            # own operator workflow; checksums detect corruption, not authenticity.
            self.run(
                [
                    *self.base,
                    "exec",
                    "-T",
                    "db",
                    "pg_restore",
                    "-U",
                    self.env["POSTGRES_USER"],
                    "-d",
                    self.env["POSTGRES_DB"],
                    "--role",
                    "detective_app",
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                ],
                input_stream=stream,
            )
        self.compose("run", "--rm", "--no-deps", "cases-init")
        for logical, filename in ARCHIVES.items():
            volume = self.volume(logical) if logical == "cases" else self.ensure_volume(logical)
            image = self.image("api") if logical == "cases" else self.image("proxy")
            command = [
                self.docker,
                "run",
                "--rm",
                "--network",
                "none",
                "-i",
                "-v",
                f"{volume}:/restore",
                "--entrypoint",
                "tar",
            ]
            if logical == "cases":
                command += ["--user", "10001:10001"]
            with (source / filename).open("rb") as stream:
                self.run([*command, image, "-C", "/restore", "-xf", "-"], input_stream=stream)
        self.start(build=False)

    def ensure_volume(self, name: str) -> str:
        value = f"{self.project}_{name}"
        self.run(
            [
                self.docker,
                "volume",
                "create",
                "--label",
                f"com.docker.compose.project={self.project}",
                "--label",
                f"com.docker.compose.volume={name}",
                value,
            ],
            capture=True,
        )
        return self.volume(name)

    def destroy_test(self) -> None:
        if not self.args.test or not self.project.startswith("detective-test-"):
            raise ValueError("Only explicitly named isolated test projects may be destroyed")
        self.compose("down", "--volumes", "--remove-orphans")


def init(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,55}", args.project):
        raise ValueError("Invalid project name")
    if any(c in args.email for c in "\r\n$`\"'"):
        raise ValueError("Invalid email")
    if (
        not (1 <= args.http_port <= 65535 and 1 <= args.https_port <= 65535)
        or args.http_port == args.https_port
    ):
        raise ValueError("Invalid ports")
    path = args.env_file.resolve()
    secret_dir = path.with_name(path.stem + ".secrets")
    if path.exists() or secret_dir.exists():
        raise ValueError("Refusing to replace existing environment or secret files")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", args.domain):
        raise ValueError("Invalid domain")
    network = ipaddress.ip_network(args.subnet)
    if network.version != 4 or network.prefixlen != 24 or not network.is_private:
        raise ValueError("Use an unused private IPv4 /24 network")
    if args.test and not args.project.startswith("detective-test-"):
        raise ValueError("Test project must start with detective-test-")
    path.parent.mkdir(parents=True, exist_ok=True)
    secret_dir.mkdir(mode=0o700)
    password = secrets.token_urlsafe(36)
    app_password = secrets.token_urlsafe(36)
    values = {
        "postgres_password": password,
        "token_hash_pepper": secrets.token_hex(48),
        "database_url": f"postgresql+asyncpg://detective_app:{app_password}@db:5432/detective",
    }
    for name, value in values.items():
        private_write(
            secret_dir / name,
            (value + "\n").encode(),
            0o600 if name == "postgres_password" else 0o444,
        )
    config = {
        "PUBLIC_DOMAIN": args.domain,
        "ACME_EMAIL": args.email,
        "POSTGRES_DB": "detective",
        "POSTGRES_USER": "detective",
        "SECRETS_DIR": str(secret_dir),
        "BIND_ADDRESS": "127.0.0.1" if args.test else "0.0.0.0",
        "HTTP_PORT": str(args.http_port),
        "HTTPS_PORT": str(args.https_port),
        "NETWORK_SUBNET": str(network),
        "NETWORK_DYNAMIC_RANGE": str(list(network.subnets(new_prefix=25))[1]),
        "PROXY_IP": str(network.network_address + 2),
        "IMAGE_TAG": "local",
    }
    private_write(
        path, ("\n".join(f"{key}={value}" for key, value in config.items()) + "\n").encode()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--test", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("init")
    setup.add_argument("--domain", required=True)
    setup.add_argument("--email", default="")
    setup.add_argument("--https-port", type=int, default=443)
    setup.add_argument("--http-port", type=int, default=80)
    setup.add_argument("--subnet", default="172.30.0.0/24")
    for name in ("start", "build", "stop", "restart-api", "destroy-test"):
        commands.add_parser(name)
    for name in ("backup", "restore"):
        command = commands.add_parser(name)
        command.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "init":
            init(args)
            return
        operation = Operations(args)
        with operation.maintenance_lock():
            if args.command == "start":
                operation.start()
            elif args.command == "build":
                operation.compose("build")
            elif args.command == "stop":
                operation.compose("stop", "proxy", "api", "db")
            elif args.command == "restart-api":
                operation.compose("restart", "api")
            elif args.command == "destroy-test":
                operation.destroy_test()
            elif args.command == "backup":
                operation.backup(args.path)
            elif args.command == "restore":
                operation.restore(args.path)
    except (OSError, ValueError, RuntimeError, tarfile.TarError, json.JSONDecodeError):
        print(
            "Operation refused or failed. No private command output was printed.", file=sys.stderr
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
