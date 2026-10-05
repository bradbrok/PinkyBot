"""Bundled roster validation, resources, and static compatibility."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.resources
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
BASE_REVISION = "00c61d40d18f5152ed2b75db76cb2406b63194e1"
BASELINE_SHA256 = "f209d414c1743d4793328de97ace1f882861dae8e969abe500b3dd5de158b648"
MAX_BYTES = 256 * 1024
PRICE_FIELDS = ("input", "output", "cached_input", "cache_write_5m", "cache_write_1h")
ROW_FIELDS = (
    "provider",
    "model_id",
    "display_name",
    "description",
    "tier",
    "context_window",
    "is_1m",
    "pricing",
    "supports_thinking",
    "active",
    "sort_order",
)
LEGACY_IDS = (
    "claude-haiku-3-5",
    "claude-opus-4",
    "claude-opus-4-1",
    "claude-sonnet-4",
    "gpt-5.3-codex",
)


def _frozen():
    return json.loads((FIXTURES / "model_roster_literals.json").read_bytes())


def _revision_one():
    return json.loads((FIXTURES / "model_roster_revision1.json").read_bytes())


def _bytes(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _loader():
    spec = importlib.util.find_spec("pinky_daemon.model_roster")
    assert spec is not None, "The bundled roster loader and strict parser are missing"
    return importlib.import_module("pinky_daemon.model_roster")


def test_revision_above_sqlite_integer_range_is_refused_at_the_field():
    document = _document()
    document["revision"] = 2**63
    failure = None
    try:
        _loader().parse(_bytes(document))
    except Exception as exc:
        failure = exc
    assert isinstance(failure, ValueError), "An unstorable revision must be refused by the parser"
    assert str(failure) == "roster.revision must be an integer in 1..9223372036854775807"


def test_maximum_sqlite_integer_revision_is_accepted():
    document = _document()
    document["revision"] = 2**63 - 1
    assert _loader().parse(_bytes(document)).revision == 2**63 - 1


def _resource(name):
    resource = importlib.resources.files("pinky_daemon").joinpath("catalog", name)
    assert resource.is_file(), f"Missing packaged catalog resource: {name}"
    return resource.read_bytes()


def _document(count=2):
    return {
        "schema": "pinky-model-roster/1",
        "revision": 1,
        "updated": "2026-10-04",
        "models": [
            {
                "provider": "anthropic",
                "model_id": f"model-{index}",
                "display_name": "Model",
                "description": "",
                "tier": "",
                "context_window": 8192,
                "is_1m": False,
                "pricing": dict.fromkeys(PRICE_FIELDS, 0),
                "supports_thinking": False,
                "active": True,
                "sort_order": 0,
            }
            for index in range(count)
        ],
    }


def _set(document, path, value):
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _assert_rejected(blob):
    loader = _loader()
    with pytest.raises(ValueError):
        loader.parse(blob)


def test_seed_tuples_equal_frozen_literals(tmp_path):
    result = _probe(tmp_path, "pinky_daemon.agent_registry", _bytes(_revision_one()), inspect=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == _frozen()["model_seeds"]


def test_rate_table_equals_frozen_literals(tmp_path):
    result = _probe(tmp_path, "pinky_daemon.pricing", _bytes(_revision_one()), inspect=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == _frozen()["rate_table"]


def test_one_million_set_equals_frozen_literals(tmp_path):
    result = _probe(tmp_path, "pinky_daemon.streaming_session", _bytes(_revision_one()), inspect=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(json.loads(result.stdout)) == set(_frozen()["one_million_models"])


@pytest.mark.parametrize("module_name,attribute", [
    ("pinky_daemon.agent_registry", "_MODEL_SEEDS"),
    ("pinky_daemon.pricing", "RATE_TABLE"),
    ("pinky_daemon.streaming_session", "_1M_MODELS"),
])
def test_imported_tables_follow_live_bundle(tmp_path, module_name, attribute):
    module = importlib.import_module(module_name)
    owner = module.AgentRegistry if attribute == "_MODEL_SEEDS" else module
    actual = getattr(owner, attribute)
    if isinstance(actual, set):
        actual = sorted(actual)
    result = _probe(tmp_path, module_name, _resource("models.json"), inspect=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == json.loads(json.dumps(actual))


@pytest.mark.parametrize("name", ["models.json", "models.schema.json", "models.baseline.json"])
def test_catalog_resources_are_importable(name):
    assert _resource(name)


def test_baseline_is_pinned_revision_one_bytes():
    baseline = _resource("models.baseline.json")
    assert hashlib.sha256(baseline).hexdigest() == BASELINE_SHA256
    assert baseline == (FIXTURES / "model_roster_revision1.json").read_bytes()
    assert json.loads(baseline) == _revision_one()


def test_load_bundled_returns_roster_dataclasses():
    loader = _loader()
    roster = loader.load_bundled()
    assert isinstance(roster, loader.Roster)
    assert dataclasses.is_dataclass(roster)
    assert all(dataclasses.is_dataclass(row) for row in roster.models)
    assert json.loads(json.dumps(dataclasses.asdict(roster))) == json.loads(_resource("models.json"))


def test_live_bundle_passes_strict_parser():
    assert _loader().parse(_resource("models.json")) == _loader().load_bundled()


def test_live_bundle_revision_is_not_below_baseline():
    live = _loader().parse(_resource("models.json"))
    baseline = _loader().parse(_resource("models.baseline.json"))
    assert live.revision >= baseline.revision


def test_live_bundle_retains_every_baseline_id():
    live = _loader().parse(_resource("models.json"))
    baseline = _loader().parse(_resource("models.baseline.json"))
    assert {(row.provider, row.model_id) for row in baseline.models} <= {
        (row.provider, row.model_id) for row in live.models
    }


def test_live_bundle_satisfies_packaged_schema():
    from jsonschema import Draft202012Validator

    schema = json.loads(_resource("models.schema.json"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(
        json.loads(_resource("models.json"))
    )


def test_recorded_generator_reproduces_revision_one(tmp_path):
    root = Path(__file__).resolve().parents[1]
    generator = root / "scripts" / "generate_model_roster.py"
    assert generator.is_file(), "The revision-one generation record is missing"
    checkout = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if checkout.returncode != 0 or checkout.stdout.strip() != "true":
        pytest.skip("not a git checkout; the revision-one generator needs repository history")
    shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if shallow.stdout.strip() == "true":
        base = subprocess.run(
            ["git", "cat-file", "-e", f"{BASE_REVISION}^{{commit}}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if base.returncode != 0:
            pytest.skip(
                f"revision-one base {BASE_REVISION[:12]} is not in this shallow checkout; "
                "the frozen-literal equality tests pin the same values"
            )
    output = tmp_path / "generated.json"
    home = tmp_path / "generator-home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
    env["HOME"] = str(home)
    result = subprocess.run(
        [
            "/usr/bin/env", "-i", *[f"{key}={value}" for key, value in env.items()],
            sys.executable,
            "-c",
            "import importlib.util, sys; p=sys.argv.pop(1); "
            "s=importlib.util.spec_from_file_location('roster_generation_record',p); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.main()",
            str(generator),
            "--base",
            BASE_REVISION,
            "--updated",
            "2026-10-04",
            "--output",
            str(output),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == (FIXTURES / "model_roster_revision1.json").read_bytes()


IMPORT_PROBE = """
import importlib
import importlib.resources as resources
import json
from pathlib import Path
import sys

root, module_name, mode = sys.argv[1:]
original_files = resources.files
def redirected_files(anchor=None, **kwargs):
    package = anchor if anchor is not None else kwargs.get('package')
    name = getattr(package, '__name__', package)
    if name == 'pinky_daemon':
        return Path(root)
    if name == 'pinky_daemon.catalog':
        return Path(root) / 'catalog'
    return original_files(package)
resources.files = redirected_files
try:
    module = importlib.import_module(module_name)
except Exception as error:
    print('ROSTER_IMPORT_REJECTED:' + type(error).__name__ + ':' + str(error))
    raise SystemExit(17)
if mode == 'inspect':
    if module_name.endswith('agent_registry'):
        result = module.AgentRegistry._MODEL_SEEDS
        assert isinstance(result, list)
        assert all(isinstance(row, tuple) for row in result)
        assert all(type(row[6]) is int and type(row[10]) is int for row in result)
    elif module_name.endswith('pricing'):
        result = module.RATE_TABLE
    else:
        assert isinstance(module._1M_MODELS, set)
        result = sorted(module._1M_MODELS)
    print(json.dumps(result))
"""


def _probe(tmp_path, module_name, blob, *, inspect=False):
    resource_root = tmp_path / "resources"
    catalog = resource_root / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    for name in ("models.schema.json", "models.baseline.json"):
        resource = importlib.resources.files("pinky_daemon").joinpath("catalog", name)
        if resource.is_file():
            (catalog / name).write_bytes(resource.read_bytes())
    (catalog / "models.json").write_bytes(blob)
    package = importlib.import_module("pinky_daemon")
    source_root = Path(package.__file__).resolve().parent.parent
    home = tmp_path / "empty-home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TMPDIR"}}
    env["HOME"] = str(home)
    env.update(
        PYTHONPATH=str(source_root), PYTHONDONTWRITEBYTECODE="1", PINKY_TEST_TRANSPORT_GUARD="1"
    )
    return subprocess.run(
        [
            "/usr/bin/env", "-i", *[f"{key}={value}" for key, value in env.items()],
            sys.executable,
            "-c",
            IMPORT_PROBE,
            str(resource_root),
            module_name,
            "inspect" if inspect else "import",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    "module_name",
    [
        "pinky_daemon.agent_registry",
        "pinky_daemon.pricing",
        "pinky_daemon.streaming_session",
    ],
)
def test_corrupt_bundled_file_fails_at_import(tmp_path, module_name):
    control = _probe(tmp_path, module_name, _bytes(_revision_one()))
    assert control.returncode == 0, control.stdout + control.stderr
    rejected = _probe(tmp_path, module_name, b"{not-json")
    assert rejected.returncode == 17, "Import silently accepted corrupt bundled roster bytes"
    assert "ROSTER_IMPORT_REJECTED:" in rejected.stdout


@pytest.mark.parametrize("legacy_id", LEGACY_IDS)
def test_legacy_rate_id_in_roster_fails_at_import(tmp_path, legacy_id):
    document = _revision_one()
    control = _probe(tmp_path, "pinky_daemon.pricing", _bytes(document))
    assert control.returncode == 0, control.stdout + control.stderr
    row = _document(1)["models"][0]
    row["model_id"] = legacy_id
    row["provider"] = "openai" if legacy_id.startswith("gpt-") else "anthropic"
    document["models"].append(row)
    rejected = _probe(tmp_path, "pinky_daemon.pricing", _bytes(document))
    assert rejected.returncode == 17, f"Import silently accepted legacy overlap: {legacy_id}"
    assert "ROSTER_IMPORT_REJECTED:" in rejected.stdout


@pytest.mark.parametrize(
    "module_name",
    [
        "pinky_daemon.agent_registry",
        "pinky_daemon.pricing",
        "pinky_daemon.streaming_session",
    ],
)
def test_static_collections_read_the_bundled_file(tmp_path, module_name):
    document = _revision_one()
    control = _probe(tmp_path, module_name, _bytes(document), inspect=True)
    assert control.returncode == 0, control.stdout + control.stderr
    changed = document["models"][0]
    changed.update(display_name="Roster Display Name", context_window=8192, is_1m=False)
    changed["pricing"]["input"] = 11
    result = _probe(tmp_path, module_name, _bytes(document), inspect=True)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads(result.stdout)
    frozen = _frozen()
    if module_name.endswith("agent_registry"):
        expected = frozen["model_seeds"]
        expected[0][2], expected[0][5], expected[0][6], expected[0][7] = (
            "Roster Display Name",
            8192,
            0,
            11,
        )
    elif module_name.endswith("pricing"):
        expected = frozen["rate_table"]
        expected[changed["model_id"]]["input"] = 11
    else:
        expected = sorted(set(frozen["one_million_models"]) - {changed["model_id"]})
    assert actual == expected


BAD_FIELDS = [
    pytest.param(("schema",), "pinky-model-roster/2", id="unknown-schema"),
    pytest.param(("schema",), 1, id="schema-type"),
    *[
        pytest.param(("revision",), value, id=f"revision-{label}")
        for label, value in [
            ("zero", 0),
            ("negative", -1),
            ("bool", True),
            ("float", 1.0),
            ("string", "1"),
            ("null", None),
        ]
    ],
    *[
        pytest.param(("updated",), value, id=f"updated-{label}")
        for label, value in [
            ("calendar", "2026-02-29"),
            ("datetime", "2026-10-04T00:00:00Z"),
            ("basic", "20261004"),
            ("nonpadded", "2026-1-4"),
            ("null", None),
        ]
    ],
    *[
        pytest.param(("models", 1, "provider"), value, id=f"provider-{label}")
        for label, value in [("unknown", "other"), ("case", "Anthropic"), ("type", 1)]
    ],
    *[
        pytest.param(("models", 1, "model_id"), value, id=f"id-{label}")
        for label, value in [
            ("empty", ""),
            ("uppercase", "Model"),
            ("tier", "model[1m]"),
            ("slash", "anthropic/model"),
            ("space", "model id"),
            ("newline", "model\n"),
            ("start", "_model"),
            ("long", "m" * 101),
            ("type", 1),
        ]
    ],
    *[
        pytest.param(("models", 1, "context_window"), value, id=f"context-{label}")
        for label, value in [("low", 8191), ("bool", True), ("float", 8192.0), ("string", "8192")]
    ],
    *[
        pytest.param(("models", 1, "sort_order"), value, id=f"sort-{label}")
        for label, value in [
            ("low", -1),
            ("high", 10001),
            ("bool", True),
            ("float", 1.0),
            ("string", "1"),
        ]
    ],
    *[
        pytest.param(("models", 1, field), value, id=f"{field}-{label}")
        for field in ["is_1m", "supports_thinking", "active"]
        for label, value in [("integer", 0), ("string", "false"), ("null", None)]
    ],
    *[
        pytest.param(("models", 1, field), value, id=f"{field}-{label}")
        for field, maximum in [("display_name", 100), ("description", 500), ("tier", 40)]
        for label, value in [("long", "x" * (maximum + 1)), ("type", 1)]
    ],
    pytest.param(("models", 1, "display_name"), "", id="display-empty"),
    pytest.param(("models",), {}, id="models-not-array"),
    pytest.param(("models", 1), [], id="row-not-object"),
    pytest.param(("models", 1, "pricing"), [], id="pricing-not-object"),
]


@pytest.mark.parametrize("path,value", BAD_FIELDS)
def test_invalid_field_rejects_whole_document(path, value):
    document = _document()
    _set(document, path, value)
    _assert_rejected(_bytes(document))


@pytest.mark.parametrize("field", PRICE_FIELDS)
@pytest.mark.parametrize(
    "value", [True, "1", None, -0.001, 1000.001, float("nan"), float("inf"), float("-inf")]
)
def test_invalid_price_rejects_whole_document(field, value):
    document = _document()
    document["models"][1]["pricing"][field] = value
    _assert_rejected(_bytes(document))


@pytest.mark.parametrize(
    "level,key",
    [
        *[((), key) for key in ("schema", "revision", "updated", "models")],
        *[(("models", 1), key) for key in ROW_FIELDS],
        *[(("models", 1, "pricing"), key) for key in PRICE_FIELDS],
    ],
)
def test_missing_required_key_rejects_whole_document(level, key):
    document = _document()
    target = document
    for part in level:
        target = target[part]
    del target[key]
    _assert_rejected(_bytes(document))


@pytest.mark.parametrize("level", [(), ("models", 1), ("models", 1, "pricing")])
def test_unknown_key_rejects_whole_document(level):
    document = _document()
    target = document
    for part in level:
        target = target[part]
    target["unexpected"] = 0
    _assert_rejected(_bytes(document))


@pytest.mark.parametrize("other_provider", ["anthropic", "openai"])
def test_duplicate_bare_id_rejects_across_providers(other_provider):
    document = _document()
    document["models"][1].update(model_id="model-0", provider=other_provider)
    _assert_rejected(_bytes(document))


@pytest.mark.parametrize("context,is_1m", [(999999, True), (1000000, False), (10000001, True)])
def test_context_flag_and_upper_bound_reject_whole_document(context, is_1m):
    document = _document()
    document["models"][1].update(context_window=context, is_1m=is_1m)
    _assert_rejected(_bytes(document))


@pytest.mark.parametrize("blob", [b"{", b"{} trailing", b"\xff", b"null", b"[]"])
def test_invalid_json_document_is_rejected(blob):
    _assert_rejected(blob)


def test_model_count_limit_rejects_whole_document():
    blob = _bytes(_document(501))
    assert len(blob) < MAX_BYTES
    _assert_rejected(blob)


def test_document_byte_limit_rejects_whole_document():
    blob = _bytes(_document())
    _assert_rejected(blob + b" " * (MAX_BYTES + 1 - len(blob)))


def test_document_limit_counts_utf8_bytes():
    document = _document(500)
    for row in document["models"]:
        row["description"] = "é" * 150
    blob = _bytes(document)
    assert len(blob.decode("utf-8")) <= MAX_BYTES < len(blob)
    _assert_rejected(blob)


def test_empty_model_roster_is_rejected():
    _assert_rejected(_bytes(_document(0)))


@pytest.mark.parametrize(
    "original,duplicate",
    [
        pytest.param(b'"revision":1', b'"revision":1,"revision":2', id="document"),
        pytest.param(
            b'"display_name":"Model"', b'"display_name":"Before","display_name":"After"', id="model"
        ),
        pytest.param(b'"input":0', b'"input":999,"input":1', id="pricing"),
    ],
)
def test_duplicate_json_key_is_rejected_at_every_level(original, duplicate):
    loader = _loader()
    blob = _bytes(_document())
    assert loader.parse(blob).models
    assert original in blob
    with pytest.raises(ValueError, match="duplicate"):
        loader.parse(blob.replace(original, duplicate, 1))


def test_excessive_json_nesting_raises_value_error():
    blob = b"[" * 100000 + b"]" * 100000
    assert len(blob) <= MAX_BYTES
    _assert_rejected(blob)


@pytest.mark.parametrize("count", [1, 500])
def test_model_count_boundaries_are_accepted(count):
    document = _document(count)
    blob = _bytes(document)
    assert len(blob) <= MAX_BYTES
    assert len(_loader().parse(blob).models) == count


def test_exact_document_byte_limit_is_accepted():
    blob = _bytes(_document())
    roster = _loader().parse(blob + b" " * (MAX_BYTES - len(blob)))
    assert len(roster.models) == 2


@pytest.mark.parametrize(
    "context,is_1m", [(8192, False), (999999, False), (1000000, True), (10000000, True)]
)
def test_valid_context_boundaries_are_accepted(context, is_1m):
    document = _document()
    document["models"][1].update(context_window=context, is_1m=is_1m)
    roster = _loader().parse(_bytes(document))
    assert roster.models[1].context_window == context
    assert roster.models[1].is_1m is is_1m


@pytest.mark.parametrize("value", [0, 0.0, 1000, 1000.0])
def test_price_boundaries_are_accepted(value):
    document = _document()
    document["models"][1]["pricing"] = dict.fromkeys(PRICE_FIELDS, value)
    roster = _loader().parse(_bytes(document))
    assert dataclasses.asdict(roster.models[1])["pricing"] == dict.fromkeys(PRICE_FIELDS, value)


def test_string_and_sort_boundaries_are_accepted():
    document = _document()
    document["updated"] = "2024-02-29"
    document["models"][1].update(
        provider="openai",
        model_id="m" * 100,
        display_name="界" * 100,
        description="界" * 500,
        tier="界" * 40,
        sort_order=10000,
        supports_thinking=True,
        active=False,
    )
    roster = _loader().parse(_bytes(document))
    assert json.loads(json.dumps(dataclasses.asdict(roster))) == document


def test_schema_is_draft_2020_12_and_accepts_revision_one():
    from jsonschema import Draft202012Validator

    schema = json.loads(_resource("models.schema.json"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(
        _revision_one()
    )


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema",), "unknown"),
        (("revision",), 0),
        (("updated",), "2026-02-29"),
        (("models", 1, "provider"), "other"),
        (("models", 1, "model_id"), "model[1m]"),
        (("models", 1, "context_window"), 8191),
        (("models", 1, "is_1m"), True),
        (("models", 1, "pricing", "input"), True),
        (("models", 1, "pricing", "output"), -1),
        (("models", 1, "pricing", "cached_input"), 1001),
        (("models", 1, "pricing", "cache_write_5m"), "1"),
        (("models", 1, "pricing", "cache_write_1h"), None),
        (("models", 1, "display_name"), ""),
        (("models", 1, "description"), "x" * 501),
        (("models", 1, "tier"), "x" * 41),
        (("models", 1, "sort_order"), 10001),
        (("models", 1, "supports_thinking"), 1),
        (("models", 1, "active"), 1),
    ],
)
def test_schema_rejects_representable_field_violations(path, value):
    from jsonschema import Draft202012Validator

    document = _document()
    _set(document, path, value)
    schema = json.loads(_resource("models.schema.json"))
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    assert list(validator.iter_errors(document)), "Schema silently accepted an invalid field"


@pytest.mark.parametrize("level", [(), ("models", 1), ("models", 1, "pricing")])
def test_schema_rejects_unknown_keys(level):
    from jsonschema import Draft202012Validator

    document = _document()
    target = document
    for part in level:
        target = target[part]
    target["unexpected"] = 0
    validator = Draft202012Validator(json.loads(_resource("models.schema.json")))
    assert list(validator.iter_errors(document))
