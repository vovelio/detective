import copy
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
from detective.scenarios import validation as v
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def case():
    return json.loads((ROOT / "docs/examples/demo-case.json").read_text())


def package(case, extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("case.json", json.dumps(case))
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return output.getvalue()


def test_demo(case):
    assert v.validate_case(case) == case
    assert v.validate_package((ROOT / "docs/examples/demo-case.detective").read_bytes()) == case


def test_schema_copy():
    assert (ROOT / "docs/schema/detective-case.schema.json").read_bytes() == (
        ROOT / "backend/detective/scenarios/detective-case.schema.json"
    ).read_bytes()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(format_version="2.0.0"),
        lambda c: c["play_cards"][0].update(id=c["start_card"]["id"]),
        lambda c: c["questions"][0].update(correct_option_id="e"),
        lambda c: c["questions"][0]["options"][1].update(id="a"),
        lambda c: c["questions"][0].update(evidence_card_ids=["missing", "clue-01"]),
        lambda c: c["play_cards"][0].update(supports_questions=["missing"]),
        lambda c: c["start_card"].update(
            blocks=[{"type": "image", "asset_id": "missing", "caption": "Фото"}]
        ),
        lambda c: c["meta"].update(min_players=8, max_players=2),
        lambda c: c.update(play_cards=c["play_cards"][:-1]),
        lambda c: c["play_cards"][-1].update(role="supporting"),
        lambda c: c["scoring"].update(points_per_correct=3),
        lambda c: c["meta"].update(original_work=False),
    ],
)
def test_invalid_semantics(case, mutation):
    mutation(case)
    with pytest.raises(v.ValidationError) as error:
        v.validate_case(case)
    assert error.value.issues[0].path.startswith("/")


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "assets/../x",
        "assets\\x",
        "assets/a/b.png",
        "assets/x.html",
        "nested.zip",
    ],
)
def test_unsafe_files(case, name):
    with pytest.raises(v.ValidationError):
        v.validate_package(package(case, {name: b"bad"}))


def test_duplicate_and_symlink(case):
    for symlink in (False, True):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("case.json", json.dumps(case))
            info = zipfile.ZipInfo("assets/link" if symlink else "case.json")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            if symlink:
                archive.writestr(info, "outside")
            else:
                with pytest.warns(UserWarning):
                    archive.writestr(info, "duplicate")
        with pytest.raises(v.ValidationError):
            v.validate_package(output.getvalue())


@pytest.mark.parametrize("data", [b"bad", b"PK\x03\x04broken", b"", b"\xff"])
def test_bad_zip(data):
    with pytest.raises(v.ValidationError):
        v.validate_package(data)


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b'{"x":NaN}', b"{", b"\xff"])
def test_bad_json(data):
    with pytest.raises(v.ValidationError):
        v.load_case(data)


@pytest.mark.parametrize("limit", ["MAX_ZIP_BYTES", "MAX_UNPACKED_BYTES", "MAX_FILES"])
def test_limits(case, monkeypatch, limit):
    monkeypatch.setattr(v, limit, 1)
    data = package(case, {"extra": b"x"})
    with pytest.raises(v.ValidationError):
        v.validate_package(data)


def test_assets(case):
    case["assets"] = [
        {"id": "photo-01", "path": "assets/photo.png", "mime": "image/png", "alt": "Фото"}
    ]
    case["start_card"]["blocks"] = [{"type": "image", "asset_id": "photo-01", "caption": "Фото"}]
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG")
    assert v.validate_package(package(case, {"assets/photo.png": output.getvalue()})) == case
    for content in (b"<script>bad</script>", output.getvalue()[:20]):
        with pytest.raises(v.ValidationError):
            v.validate_package(package(case, {"assets/photo.png": content}))
    with pytest.raises(v.ValidationError):
        v.validate_package(package(case))
    broken = copy.deepcopy(case)
    broken["assets"][0]["mime"] = "image/jpeg"
    with pytest.raises(v.ValidationError):
        v.validate_package(package(broken, {"assets/photo.png": output.getvalue()}))


def test_error_does_not_include_secret(case):
    case["solution"]["full_story"] = {"secret": "never-log-this"}
    with pytest.raises(v.ValidationError) as error:
        v.validate_case(case)
    assert "never-log-this" not in str(error.value)
