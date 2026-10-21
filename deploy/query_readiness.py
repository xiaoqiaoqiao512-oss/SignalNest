"""Read-only query deployment checks; never connect to a model or read credentials."""

import argparse
import json
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError

from signalnest.config import ConfigurationError
from signalnest.querying.config import load_query_config, load_query_inputs
from signalnest.querying.contracts import QueryError
from signalnest.querying.snapshots import validate_snapshot
from signalnest.search import SearchError, SearchQuery, search_notices
from signalnest.storage import (
    StorageError,
    make_engine,
    migration_config,
    open_initialized_engine,
)

_SNAPSHOT_ERRORS = frozenset(
    {
        "snapshot_path_unsafe",
        "snapshot_unavailable",
        "snapshot_manifest_invalid",
        "snapshot_binding_mismatch",
        "snapshot_database_not_frozen",
        "snapshot_database_changed",
        "snapshot_sending_not_paused",
    }
)


def _instance_readiness(config, settings):
    result = {
        "instance_id": config.id,
        "ready": False,
        "storage_readable": False,
        "schema_ready": False,
        "index_ready": False,
        "snapshot_required": config.snapshot_manifest is not None,
        "snapshot_verified": False,
        "error_code": None,
    }
    engine = None
    try:
        # Inspect the existing revision without relying on a writable connection or
        # telling the operator to migrate a production instance implicitly.
        engine = make_engine(settings.storage.database, must_exist=True, read_only=True)
        with engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()
        result["storage_readable"] = True
        scripts = ScriptDirectory.from_config(migration_config())
        expected = scripts.get_current_head()
        if revision != expected:
            if revision is None:
                result["error_code"] = "query_storage_uninitialized"
            else:
                known = {item.revision for item in scripts.walk_revisions()}
                result["error_code"] = (
                    "query_schema_upgrade_required"
                    if revision in known
                    else "query_schema_incompatible"
                )
            return result
        result["schema_ready"] = True
        engine.dispose()
        engine = None
        if config.snapshot_manifest is not None:
            validate_snapshot(
                config.snapshot_manifest,
                settings.storage.database,
                config.id,
                settings.source.id,
            )
            result["snapshot_verified"] = True
        engine = open_initialized_engine(settings.storage.database, read_only=True)
        # A literal three-character term exercises the actual FTS query path and
        # its freshness check. Hits and notice content never enter this report.
        search_notices(
            engine,
            SearchQuery(query="部署检查词", source_id=settings.source.id, limit=1),
        )
        result.update(ready=True, index_ready=True)
    except QueryError as exc:
        result["error_code"] = (
            exc.code if exc.code in _SNAPSHOT_ERRORS else "query_snapshot_invalid"
        )
    except SearchError as exc:
        result["error_code"] = (
            exc.code
            if exc.code in {"search_index_missing", "search_index_stale"}
            else "query_index_unavailable"
        )
    except (SQLAlchemyError, StorageError, CommandError):
        result["error_code"] = "query_storage_unavailable"
    finally:
        if engine is not None:
            engine.dispose()
    return result


def inspect_query_readiness(config_path: Path, *, require_model: bool = False):
    """Return readiness of configured local inputs, not API or model compatibility."""
    result = {
        "format": "signalnest-query-readiness-v1",
        "local_ready": False,
        "ready": False,
        "model_configured": False,
        "model_required": require_model,
        "model_runtime_verified": False,
        "credentials_checked": False,
        "network_checked": False,
        "instances": [],
        "diagnostic_codes": [],
    }
    try:
        settings = load_query_config(config_path)
        inputs, _ = load_query_inputs(settings)
    except ConfigurationError:
        result["diagnostic_codes"] = ["query_configuration_invalid"]
        return result
    result["model_configured"] = settings.model is not None
    if settings.model is None:
        result["diagnostic_codes"].append("model_not_configured")
    for config in settings.instances:
        item = _instance_readiness(config, inputs[config.id])
        result["instances"].append(item)
        if item["error_code"] is not None:
            result["diagnostic_codes"].append(item["error_code"])
    result["local_ready"] = all(item["ready"] for item in result["instances"])
    result["ready"] = result["local_ready"] and (not require_model or settings.model is not None)
    result["diagnostic_codes"] = sorted(set(result["diagnostic_codes"]))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--require-model", action="store_true")
    args = parser.parse_args(argv)
    result = inspect_query_readiness(args.config, require_model=args.require_model)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if "query_configuration_invalid" in result["diagnostic_codes"]:
        return 2
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
