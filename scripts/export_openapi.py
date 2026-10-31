"""Write the canonical OpenAPI spec the web and mobile clients generate from.

Run:  uv run python scripts/export_openapi.py          (writes openapi/openapi.json)
      uv run python scripts/export_openapi.py --check  (exit 1 if the file is stale)

The file is committed so client repos can generate from a pinned, reviewed contract
(e.g. raw.githubusercontent.com/avpratap/drs-backend/main/openapi/openapi.json), and a
test fails CI whenever an API change lands without re-exporting it.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # `app` when run by path

OUT = ROOT / "openapi" / "openapi.json"


def render() -> str:
    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    spec = render()
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != spec:
            print("openapi/openapi.json is stale: run `uv run python scripts/export_openapi.py`")
            return 1
        print("openapi/openapi.json is current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(spec)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
