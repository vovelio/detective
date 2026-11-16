"""Offline safety checks: corrupt backups cannot reach Docker or existing data."""

import argparse
import importlib.util
import io
import json
import stat
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("detective_ops_test", ROOT / "infra/ops.py")
OPS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OPS)


def setup_args(tmp_path, **changes):
    values = dict(
        env_file=tmp_path / "test.env",
        project="detective-test-offline",
        test=True,
        domain="localhost",
        email="",
        https_port=18443,
        http_port=18080,
        subnet="172.29.90.0/24",
    )
    return argparse.Namespace(**(values | changes))


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hardlink", tarfile.LNKTYPE),
        ("device", tarfile.CHRTYPE),
    ],
)
def test_backup_tar_rejects_paths_links_and_devices(tmp_path, name, kind):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as stream:
        item = tarfile.TarInfo(name)
        item.type = kind
        item.linkname = "../escape"
        stream.addfile(item)
    with pytest.raises(ValueError, match="Unsafe"):
        OPS.validate_tar(archive)
    assert not (tmp_path / "escape").exists()


def test_regular_backup_tar_is_accepted_without_extraction(tmp_path):
    archive = tmp_path / "good.tar"
    with tarfile.open(archive, "w") as stream:
        item = tarfile.TarInfo("a" * 64 + ".detective")
        item.size = 3
        stream.addfile(item, io.BytesIO(b"zip"))
    OPS.validate_tar(archive)
    assert not (tmp_path / ("a" * 64 + ".detective")).exists()


def test_init_protects_secrets_and_never_overwrites(tmp_path):
    args = setup_args(tmp_path)
    OPS.init(args)
    private = tmp_path / "test.secrets"
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(args.env_file.stat().st_mode) == 0o600
    original = OPS.file_hash(private / "token_hash_pepper")
    public = args.env_file.read_text()
    assert "BIND_ADDRESS=127.0.0.1" in public
    assert "TOKEN_HASH_PEPPER=" not in public and "POSTGRES_PASSWORD=" not in public
    with pytest.raises(ValueError, match="Refusing"):
        OPS.init(args)
    assert OPS.file_hash(private / "token_hash_pepper") == original


def test_test_init_refuses_production_project_before_creating_files(tmp_path):
    with pytest.raises(ValueError, match="Test project"):
        OPS.init(setup_args(tmp_path, project="detective-production"))
    assert not list(tmp_path.iterdir())


def test_corrupt_backup_rejected_before_any_docker_operation(tmp_path, monkeypatch):
    args = setup_args(tmp_path)
    OPS.init(args)
    monkeypatch.setattr(OPS.shutil, "which", lambda _: "/unused/docker")
    operation = OPS.Operations(args)
    backup = tmp_path / "corrupt"
    backup.mkdir()
    (backup / "manifest.json").write_text(json.dumps({"backup_version": 1, "sha256": {}}))

    def forbidden(*args, **kwargs):
        raise AssertionError("Docker must not run before backup validation")

    monkeypatch.setattr(operation, "run", forbidden)
    with pytest.raises(ValueError, match="incomplete"):
        operation.restore(backup)
