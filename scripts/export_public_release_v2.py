from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 2
MAX_SECRET_SCAN_BYTES = 5 * 1024 * 1024
TEXT_RELEASE_SUFFIXES = {
    ".cmd",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".txt",
}


class ReleaseConfigError(ValueError):
    pass


class ReleaseSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContentPattern:
    name: str
    pattern: str


@dataclass(frozen=True)
class ReleaseManifest:
    include: tuple[str, ...]
    preserve: tuple[str, ...]
    deny: tuple[str, ...]
    ignore: tuple[str, ...]
    forbidden_content_patterns: tuple[ContentPattern, ...]


@dataclass(frozen=True)
class ReleasePlan:
    selected: tuple[str, ...]
    added: tuple[str, ...]
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    unmanaged: tuple[str, ...]
    missing: tuple[str, ...]
    blocked: tuple[str, ...]

    @property
    def has_drift(self) -> bool:
        return bool(self.added or self.changed or self.unmanaged or self.missing)

    @property
    def is_safe(self) -> bool:
        return not self.missing and not self.blocked

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["has_drift"] = self.has_drift
        payload["is_safe"] = self.is_safe
        return payload


def _string_list(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ReleaseConfigError(f"{key} must be a list of non-empty strings")
    return tuple(value)


def _validate_pattern(pattern: str, key: str) -> None:
    normalized = pattern.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ReleaseConfigError(f"Unsafe {key} pattern: {pattern}")


def load_manifest(path: Path) -> ReleaseManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseConfigError(f"Cannot read release manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseConfigError("Release manifest root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseConfigError(f"schema_version must be {SCHEMA_VERSION}")

    include = _string_list(payload, "include")
    preserve = _string_list(payload, "preserve")
    deny = _string_list(payload, "deny")
    ignore = _string_list(payload, "ignore")
    if not include:
        raise ReleaseConfigError("include must not be empty")
    for key, patterns in (
        ("include", include),
        ("preserve", preserve),
        ("deny", deny),
        ("ignore", ignore),
    ):
        for pattern in patterns:
            _validate_pattern(pattern, key)

    raw_content_patterns = payload.get("forbidden_content_patterns", [])
    if not isinstance(raw_content_patterns, list):
        raise ReleaseConfigError("forbidden_content_patterns must be a list")
    content_patterns: list[ContentPattern] = []
    for item in raw_content_patterns:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("pattern"), str):
            raise ReleaseConfigError("Each forbidden content pattern requires string name and pattern")
        try:
            re.compile(item["pattern"])
        except re.error as exc:
            raise ReleaseConfigError(f"Invalid content pattern {item['name']}: {exc}") from exc
        content_patterns.append(ContentPattern(name=item["name"], pattern=item["pattern"]))

    return ReleaseManifest(
        include=include,
        preserve=preserve,
        deny=deny,
        ignore=ignore,
        forbidden_content_patterns=tuple(content_patterns),
    )


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _matches(path: str, patterns: Iterable[str]) -> bool:
    lowered = path.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern.replace("\\", "/").lower()) for pattern in patterns)


def _expand_includes(source_root: Path, patterns: Iterable[str]) -> tuple[set[str], list[str], list[str]]:
    selected: set[str] = set()
    missing: list[str] = []
    blocked: list[str] = []
    for pattern in patterns:
        matches = sorted(source_root.glob(pattern))
        file_matches = [path for path in matches if path.is_file() or path.is_symlink()]
        if not file_matches:
            missing.append(pattern)
            continue
        for path in file_matches:
            resolved = path.resolve()
            if path.is_symlink() or not resolved.is_relative_to(source_root):
                blocked.append(f"symlink_or_escape:{_relative_posix(path, source_root)}")
                continue
            selected.add(_relative_posix(path, source_root))
    return selected, missing, blocked


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_release_content(source: Path, public: Path) -> bool:
    if _sha256(source) == _sha256(public):
        return True
    if source.suffix.lower() not in TEXT_RELEASE_SUFFIXES and source.name != "requirements.txt":
        return False
    source_bytes = source.read_bytes().replace(b"\r\n", b"\n")
    public_bytes = public.read_bytes().replace(b"\r\n", b"\n")
    return source_bytes == public_bytes


def _scan_content(path: Path, relative_path: str, patterns: Iterable[ContentPattern]) -> list[str]:
    if path.stat().st_size > MAX_SECRET_SCAN_BYTES:
        return [f"oversized_for_secret_scan:{relative_path}"]
    data = path.read_bytes()
    if b"\x00" in data:
        return [f"binary_file:{relative_path}"]
    text = data.decode("utf-8", errors="replace")
    return [
        f"forbidden_content:{item.name}:{relative_path}"
        for item in patterns
        if re.search(item.pattern, text)
    ]


def _public_files(public_root: Path, ignore: Iterable[str]) -> set[str]:
    files: set[str] = set()
    for path in public_root.rglob("*"):
        relative = _relative_posix(path, public_root)
        if relative == ".git" or relative.startswith(".git/"):
            continue
        if _matches(relative, ignore):
            continue
        if path.is_file() or path.is_symlink():
            files.add(relative)
    return files


def build_plan(source_root: Path, public_root: Path, manifest: ReleaseManifest) -> ReleasePlan:
    source_root = source_root.resolve()
    public_root = public_root.resolve()
    if not source_root.is_dir():
        raise ReleaseConfigError(f"Source root does not exist: {source_root}")
    if not public_root.is_dir():
        raise ReleaseConfigError(f"Public root does not exist: {public_root}")
    if source_root == public_root or public_root.is_relative_to(source_root):
        raise ReleaseSafetyError("Public root must be outside the source tree")

    selected, missing, blocked = _expand_includes(source_root, manifest.include)
    for relative in sorted(selected):
        if _matches(relative, manifest.deny):
            blocked.append(f"denied_source_path:{relative}")
            continue
        blocked.extend(
            _scan_content(
                source_root / Path(relative),
                relative,
                manifest.forbidden_content_patterns,
            )
        )

    public_files = _public_files(public_root, manifest.ignore)
    for relative in sorted(public_files):
        if _matches(relative, manifest.deny):
            blocked.append(f"denied_public_path:{relative}")

    added: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    for relative in sorted(selected):
        source_path = source_root / Path(relative)
        public_path = public_root / Path(relative)
        if not public_path.exists():
            added.append(relative)
        elif public_path.is_symlink() or not public_path.is_file():
            blocked.append(f"invalid_public_target:{relative}")
        elif _same_release_content(source_path, public_path):
            unchanged.append(relative)
        else:
            changed.append(relative)

    unmanaged = sorted(
        relative
        for relative in public_files - selected
        if not _matches(relative, manifest.preserve)
    )
    return ReleasePlan(
        selected=tuple(sorted(selected)),
        added=tuple(added),
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        unmanaged=tuple(unmanaged),
        missing=tuple(sorted(set(missing))),
        blocked=tuple(sorted(set(blocked))),
    )


def _git_status(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseSafetyError(f"Not a usable Git working tree: {root}: {result.stderr.strip()}")
    return result.stdout.strip()


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def apply_plan(
    source_root: Path,
    public_root: Path,
    plan: ReleasePlan,
    *,
    prune: bool,
    require_clean_source: bool = True,
    require_clean_public: bool = True,
) -> None:
    if not plan.is_safe:
        raise ReleaseSafetyError("Refusing to sync an unsafe release plan")
    if plan.unmanaged and not prune:
        raise ReleaseSafetyError("Unmanaged public files exist; review them and rerun with --prune")
    if require_clean_source and _git_status(source_root):
        raise ReleaseSafetyError("Source Git working tree is dirty; commit or stash before syncing")
    if require_clean_public and _git_status(public_root):
        raise ReleaseSafetyError("Public Git working tree is dirty; commit or stash before syncing")

    for relative in (*plan.added, *plan.changed):
        _atomic_copy(source_root / Path(relative), public_root / Path(relative))
    if prune:
        for relative in plan.unmanaged:
            target = public_root / Path(relative)
            if target.is_symlink() or target.is_file():
                target.unlink()
        for directory in sorted(
            (path for path in public_root.rglob("*") if path.is_dir() and ".git" not in path.parts),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass


def _print_plan(plan: ReleasePlan) -> None:
    print("PUBLIC_RELEASE_CHECK_V2")
    print(
        " ".join(
            (
                f"selected={len(plan.selected)}",
                f"added={len(plan.added)}",
                f"changed={len(plan.changed)}",
                f"unchanged={len(plan.unchanged)}",
                f"unmanaged={len(plan.unmanaged)}",
                f"missing={len(plan.missing)}",
                f"blocked={len(plan.blocked)}",
            )
        )
    )
    for label, paths in (
        ("ADD", plan.added),
        ("CHANGE", plan.changed),
        ("UNMANAGED", plan.unmanaged),
        ("MISSING", plan.missing),
        ("BLOCKED", plan.blocked),
    ):
        for path in paths:
            print(f"[{label}] {path}")


def _write_report(path: Path, plan: ReleasePlan, source_root: Path, public_root: Path) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_root": str(source_root.resolve()),
        "public_root": str(public_root.resolve()),
        "plan": plan.to_dict(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Safely check or sync the curated public repository")
    parser.add_argument("--source-root", type=Path, default=project_root)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=project_root / "configs" / "public_release_v2.json",
    )
    parser.add_argument("--public-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Report drift without writing")
    mode.add_argument("--sync", action="store_true", help="Copy approved source files")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="With --sync, remove public files not selected or preserved",
    )
    parser.add_argument(
        "--allow-dirty-source",
        action="store_true",
        help="With --sync, publish reviewed working-tree content while still requiring a clean public repo",
    )
    parser.add_argument("--report-json", type=Path)
    args = parser.parse_args(argv)
    if args.prune and not args.sync:
        parser.error("--prune requires --sync")
    if args.allow_dirty_source and not args.sync:
        parser.error("--allow-dirty-source requires --sync")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        plan = build_plan(args.source_root, args.public_dir, manifest)
        _print_plan(plan)
        if args.report_json:
            _write_report(args.report_json, plan, args.source_root, args.public_dir)
        if not plan.is_safe:
            return 2
        if args.check:
            return 1 if plan.has_drift else 0
        apply_plan(
            args.source_root.resolve(),
            args.public_dir.resolve(),
            plan,
            prune=args.prune,
            require_clean_source=not args.allow_dirty_source,
        )
        verified = build_plan(args.source_root, args.public_dir, manifest)
        if not verified.is_safe or verified.added or verified.changed or verified.unmanaged:
            raise ReleaseSafetyError("Post-sync verification failed")
        print("SYNC_VERIFIED")
        return 0
    except (ReleaseConfigError, ReleaseSafetyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
