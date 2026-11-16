"""Local package validation; never prints scenario content."""

import sys
from pathlib import Path

from .validation import MAX_ZIP_BYTES, ValidationError, validate_package


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m detective.scenarios file.detective", file=sys.stderr)
        return 2
    try:
        with Path(sys.argv[1]).open("rb") as stream:
            data = stream.read(MAX_ZIP_BYTES + 1)
        validate_package(data)
    except ValidationError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Cannot read package", file=sys.stderr)
        return 1
    print("Valid detective-case 1.0.0 package")
    return 0


if __name__ == "__main__":
    sys.exit(main())
