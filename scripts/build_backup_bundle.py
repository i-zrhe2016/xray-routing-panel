#!/usr/bin/env python3
"""Build a portable, metadata-rich disaster-recovery archive.

The archive is deliberately a slow/offline recovery artifact.  It is not
mounted by the running panel and it does not attempt to make failover faster.
The caller can add configuration files and directories through
``DB_BACKUP_EXTRA_PATHS``; the SQLite snapshot is always included.
"""

import argparse
import glob
import hashlib
import io
import json
import os
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from node_recovery import NODE_RECOVERY_MANIFEST_NAME, build_node_recovery_manifest
except ModuleNotFoundError:
    from scripts.node_recovery import NODE_RECOVERY_MANIFEST_NAME, build_node_recovery_manifest


STAMP_PATTERN = re.compile(r"(\d{8})T(\d{6})Z")


def parse_extra_paths(value):
    """Parse a comma/newline separated list while preserving spaces in paths."""

    if not value:
        return []
    items = []
    seen = set()
    for raw in re.split(r"[,\n]+", str(value)):
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)
    return items


def _expand_sources(items):
    expanded = []
    seen = set()
    for item in items:
        matches = glob.glob(item, recursive=True) if glob.has_magic(item) else [item]
        for match in matches or [item]:
            path = Path(match)
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            expanded.append(path)
    return expanded


def _safe_source_name(source):
    """Map an absolute source to a stable, non-traversing archive path."""

    raw = source.as_posix().lstrip("/")
    parts = [part for part in raw.split("/") if part not in {"", ".", ".."}]
    return Path("config") / Path(*parts) if parts else Path("config") / "root"


def _iter_files(source):
    if source.is_file():
        yield source
        return
    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_file() and not path.is_symlink():
                yield path


def _timestamp_for(backup_path):
    match = STAMP_PATTERN.search(Path(backup_path).stem)
    if match:
        return f"{match.group(1)}T{match.group(2)}Z"
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _archive_name(prefix, backup_path):
    safe_prefix = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(prefix or "")).strip("-")
    safe_prefix = re.sub(r"-{2,}", "-", safe_prefix) or "xray-routing-panel"
    return f"{safe_prefix}-disaster-{_timestamp_for(backup_path)}.tar.gz"


def _add_source(archive, source, arcname, file_entries):
    # Read each regular file once and use those exact bytes for both the tar
    # member and its manifest hash.  Hashing after ``archive.add`` can race
    # with a live renderer rewriting a config file and describe bytes that are
    # not the bytes in the archive.
    if source.is_dir():
        archive.add(source, arcname=str(arcname), recursive=False)
    for path in _iter_files(source):
        try:
            relative = Path(arcname) / path.relative_to(source) if source.is_dir() else Path(arcname)
            data = path.read_bytes()
            info = archive.gettarinfo(str(path), arcname=relative.as_posix())
            if not info.isreg():
                continue
            info.size = len(data)
            archive.addfile(info, fileobj=io.BytesIO(data))
            file_entries.append(
                {
                    "archivePath": relative.as_posix(),
                    "sourcePath": str(path),
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        except (FileNotFoundError, OSError):
            # A file can disappear while a live runtime directory is being
            # archived.  Skip it consistently so neither tar nor manifest
            # claims to contain bytes that were not captured.
            continue


def _add_generated_file(archive, archive_path, data, file_entries):
    """Add generated metadata and cover it with the same top-level manifest."""

    archive_path = Path(archive_path).as_posix()
    info = tarfile.TarInfo(archive_path)
    info.size = len(data)
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    archive.addfile(info, fileobj=io.BytesIO(data))
    file_entries.append(
        {
            "archivePath": archive_path,
            "sourcePath": f"<generated>/{archive_path}",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )


def _read_remote_collection_manifest(named_sources):
    for source, archive_name in named_sources:
        if Path(archive_name) != Path("nodes"):
            continue
        manifest_path = Path(source) / "remote-node-collection.json"
        if not manifest_path.is_file():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
    return None


def create_backup_bundle(backup_path, extra_paths, bundle_dir, prefix, named_paths=()):
    """Create a gzip tar archive and return its final path.

    Missing optional paths are recorded as skipped rather than making the
    database backup fail.  This is important for the same image to work in
    local and Docker layouts where some config files are absent.
    """

    database = Path(backup_path).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"database backup not found: {database}")

    destination_dir = Path(bundle_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    final_path = destination_dir / _archive_name(prefix, database)
    temp_path = destination_dir / f".{final_path.name}.tmp"
    file_entries = []
    skipped = []
    named_sources = [(Path(source), Path(archive_name)) for source, archive_name in named_paths]
    sources = _expand_sources(
        parse_extra_paths(extra_paths) if isinstance(extra_paths, str) else (extra_paths or [])
    )

    try:
        with tarfile.open(temp_path, mode="w:gz") as archive:
            _add_source(archive, database, Path("database") / database.name, file_entries)
            for source in sources:
                resolved = source.expanduser().resolve()
                if not resolved.exists():
                    skipped.append(str(source))
                    continue
                _add_source(archive, resolved, _safe_source_name(resolved), file_entries)
            for source, archive_name in named_sources:
                resolved = source.expanduser().resolve()
                if not resolved.exists():
                    skipped.append(str(source))
                    continue
                _add_source(archive, resolved, archive_name, file_entries)

            node_manifest = build_node_recovery_manifest(
                file_entries,
                _read_remote_collection_manifest(named_sources),
            )
            node_manifest_bytes = (
                json.dumps(node_manifest, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            _add_generated_file(
                archive,
                NODE_RECOVERY_MANIFEST_NAME,
                node_manifest_bytes,
                file_entries,
            )

            manifest = {
                "version": 1,
                "purpose": "disaster-recovery",
                "recoveryMode": "offline",
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "databaseBackup": str(database),
                "requestedExtraPaths": [str(item) for item in sources],
                "namedExtraPaths": [
                    {"sourcePath": str(source), "archivePath": archive_name.as_posix()}
                    for source, archive_name in named_sources
                ],
                "skippedExtraPaths": skipped,
                "files": file_entries,
            }
            manifest_bytes = (
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            info = tarfile.TarInfo("backup-manifest.json")
            info.size = len(manifest_bytes)
            info.mtime = int(datetime.now(timezone.utc).timestamp())
            archive.addfile(info, fileobj=io.BytesIO(manifest_bytes))

        os.replace(temp_path, final_path)
        # The bundle contains credentials and rendered Xray configuration
        # before the remote uploader encrypts it; never leave it world-readable.
        os.chmod(final_path, 0o600)
        print(f"[backup] wrote disaster bundle {final_path}")
        if skipped:
            print(f"[backup] skipped {len(skipped)} missing optional path(s): {', '.join(skipped)}")
        return final_path.resolve()
    finally:
        temp_path.unlink(missing_ok=True)


def prune_bundles(bundle_dir, prefix, keep_days):
    if keep_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    removed = 0
    safe_prefix = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(prefix or "")).strip("-")
    safe_prefix = re.sub(r"-{2,}", "-", safe_prefix) or "xray-routing-panel"
    for path in Path(bundle_dir).glob(f"{safe_prefix}-disaster-*.tar.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def parse_args():
    parser = argparse.ArgumentParser(description="Build a disaster-recovery backup bundle.")
    parser.add_argument("--database-backup", required=True)
    parser.add_argument("--extra-paths", default=os.environ.get("DB_BACKUP_EXTRA_PATHS", ""))
    parser.add_argument("--bundle-dir", default=os.environ.get("DB_BACKUP_BUNDLE_DIR", "/backups"))
    parser.add_argument(
        "--prefix", default=os.environ.get("DB_BACKUP_PREFIX", "xray-routing-panel")
    )
    return parser.parse_args()


def main():
    args = parse_args()
    create_backup_bundle(args.database_backup, args.extra_paths, args.bundle_dir, args.prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
