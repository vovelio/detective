"""Validate untrusted manifests/packages without extracting or logging their contents."""

import io
import json
import stat
import warnings
import zipfile
import zlib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator
from PIL import Image

MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 80 * 1024 * 1024
MAX_FILES = 100
MAX_IMAGE_PIXELS = 16_000_000
SCHEMA = json.loads(files(__package__).joinpath("detective-case.schema.json").read_text())
Draft202012Validator.check_schema(SCHEMA)
VALIDATOR = Draft202012Validator(SCHEMA)


@dataclass(frozen=True)
class Issue:
    path: str
    code: str


class ValidationError(ValueError):
    def __init__(self, *issues: Issue):
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.code}" for i in issues))


def fail(path: str, code: str) -> None:
    raise ValidationError(Issue(path, code))


def validate_case(value: Any) -> dict[str, Any]:
    """Structural and mechanical checks; story quality requires human review."""
    issues = [
        Issue("/" + "/".join(map(str, e.absolute_path)), "SCHEMA_INVALID")
        for e in VALIDATOR.iter_errors(value)
    ]
    if issues:
        raise ValidationError(*issues)
    case: dict[str, Any] = value

    def unique(items: list[dict[str, Any]], path: str, key: str = "id") -> set[str]:
        ids: set[str] = set()
        for index, item in enumerate(items):
            if item[key] in ids:
                issues.append(Issue(f"{path}/{index}/{key}", "DUPLICATE"))
            ids.add(item[key])
        return ids

    cards = unique(case["play_cards"], "/play_cards")
    if case["start_card"]["id"] in cards:
        issues.append(Issue("/start_card/id", "DUPLICATE"))
    cards.add(case["start_card"]["id"])
    questions = unique(case["questions"], "/questions")
    assets = unique(case["assets"], "/assets")
    unique(case["assets"], "/assets", "path")
    for index, question in enumerate(case["questions"]):
        options = unique(question["options"], f"/questions/{index}/options")
        if question["correct_option_id"] not in options:
            issues.append(Issue(f"/questions/{index}/correct_option_id", "UNKNOWN_OPTION"))
        for ref in question["evidence_card_ids"]:
            if ref not in cards:
                issues.append(Issue(f"/questions/{index}/evidence_card_ids", "UNKNOWN_CARD"))
    card_paths = [
        ("/start_card", case["start_card"]),
        *((f"/play_cards/{index}", card) for index, card in enumerate(case["play_cards"])),
    ]
    for card_path, card in card_paths:
        for ref in card.get("supports_questions", []):
            if ref not in questions:
                issues.append(Issue(f"{card_path}/supports_questions", "UNKNOWN_QUESTION"))
        for block_index, block in enumerate(card["blocks"]):
            if block["type"] == "image" and block["asset_id"] not in assets:
                issues.append(Issue(f"{card_path}/blocks/{block_index}/asset_id", "UNKNOWN_ASSET"))
    meta = case["meta"]
    if meta["min_players"] > meta["max_players"]:
        issues.append(Issue("/meta/min_players", "INVALID_PLAYER_RANGE"))
    needed = 6 if meta["max_players"] == 2 else meta["max_players"] * 2
    if len(case["play_cards"]) < needed:
        issues.append(Issue("/meta/max_players", "INSUFFICIENT_CARDS"))
    if sum(c["role"] == "red_herring" for c in case["play_cards"]) < 6:
        issues.append(Issue("/play_cards", "INSUFFICIENT_RED_HERRINGS"))
    if issues:
        raise ValidationError(*issues)
    return case


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("/case.json", "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(value: str) -> None:
    fail("/case.json", "INVALID_JSON_CONSTANT")


def load_case(data: bytes) -> dict[str, Any]:
    try:
        return validate_case(
            json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        fail("/case.json", "INVALID_JSON")
    raise AssertionError("unreachable")


def _image(data: bytes, mime: str, path: str) -> None:
    expected = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[mime]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != expected or image.width * image.height > MAX_IMAGE_PIXELS:
                    fail(path, "INVALID_IMAGE")
                if getattr(image, "n_frames", 1) != 1:
                    fail(path, "ANIMATED_IMAGE_NOT_ALLOWED")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        fail(path, "INVALID_IMAGE")


def validate_package(data: bytes) -> dict[str, Any]:
    """Returns a server-only manifest. Never serves a package to players or writes files."""
    if len(data) > MAX_ZIP_BYTES:
        fail("/", "ZIP_TOO_LARGE")
    if not data.startswith(b"PK\x03\x04"):
        fail("/", "INVALID_ZIP")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES:
                fail("/", "TOO_MANY_FILES")
            if sum(i.file_size for i in entries) > MAX_UNPACKED_BYTES:
                fail("/", "UNPACKED_TOO_LARGE")
            names: set[str] = set()
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                if (
                    name in names
                    or "\\" in name
                    or "\x00" in entry.orig_filename
                    or path.is_absolute()
                    or ".." in path.parts
                    or name != str(path)
                    or len(path.parts) > 2
                ):
                    fail("/", "UNSAFE_OR_DUPLICATE_PATH")
                names.add(name)
                mode = entry.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG) or entry.is_dir():
                    fail("/", "NON_REGULAR_FILE")
                if entry.flag_bits & 1 or entry.compress_type not in (
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                ):
                    fail("/", "UNSUPPORTED_ZIP_ENTRY")
            if "case.json" not in names:
                fail("/case.json", "MISSING_MANIFEST")
            case = load_case(archive.read("case.json"))
            expected = {"case.json", *(a["path"] for a in case["assets"])}
            if names != expected:
                fail("/assets", "MISSING_OR_UNDECLARED_FILE")
            for asset in case["assets"]:
                suffix = PurePosixPath(asset["path"]).suffix
                allowed = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".webp": "image/webp",
                }
                if allowed[suffix] != asset["mime"]:
                    fail("/assets", "MIME_EXTENSION_MISMATCH")
                _image(archive.read(asset["path"]), asset["mime"], "/assets")
            return case
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError, zlib.error):
        fail("/", "INVALID_ZIP")
    raise AssertionError("unreachable")
