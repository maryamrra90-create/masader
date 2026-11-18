"""The committed OpenAPI contract (docs/openapi.v1.json) must match the running app, so a change to the
API cannot go out without the contract file being regenerated (`make openapi`) and reviewed."""
import json
from pathlib import Path

from masader.api.app import app

DOC = Path(__file__).resolve().parents[2] / "docs" / "openapi.v1.json"


def test_contract_file_matches_the_app():
    assert json.loads(DOC.read_text()) == json.loads(json.dumps(app.openapi(), sort_keys=True))


def test_every_v1_route_needs_an_api_key_except_public_pages():
    spec = app.openapi()
    for path, ops in spec["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for op in ops.values():
            names = {p["name"].lower() for p in op.get("parameters", []) if p["in"] == "header"}
            assert "x-api-key" in names, f"{path} does not declare X-API-Key"
