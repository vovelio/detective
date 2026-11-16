"""Short-lived validator process. Private manifest travels only on a private pipe."""

import json
import resource
import sys
from typing import Any

from detective.scenarios.validation import MAX_ZIP_BYTES, SCHEMA, ValidationError, validate_package

MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def property_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        names.update(value.get("properties", {}))
        for child in value.values():
            names.update(property_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(property_names(child))
    return names


def safe_path(path: str) -> str:
    allowed = property_names(SCHEMA) | {"case.json", "assets"}
    parts = path.split("/")[1:17]
    if len(path) > 128 or any(part not in allowed and not part.isdecimal() for part in parts):
        return "/"
    return "/" + "/".join(parts)


def main() -> None:
    # CPU and address-space caps supplement the parent's wall timeout. Some OSes do
    # not implement RLIMIT_AS, but the archive/pixel/output caps always apply.
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    except (OSError, ValueError):
        pass
    result: dict[str, Any]
    try:
        manifest = validate_package(sys.stdin.buffer.read(MAX_ZIP_BYTES + 1))
        result = {"manifest": manifest}
    except ValidationError as error:
        result = {
            "issues": [{"path": safe_path(i.path), "code": i.code} for i in error.issues[:20]]
        }
    except Exception:
        result = {"failed": True}
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = b'{"limited":true}'
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
