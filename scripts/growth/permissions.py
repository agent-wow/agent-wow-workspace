"""0700/0600 enforcement and audits for runtime state and account files.

Tests use temp directories; production scripts call these helpers only on the
runtime tree they own. The tests never chmod the real ``accounts/`` tree.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600


def ensure_private_dir(path: str | os.PathLike[str], mode: int = PRIVATE_DIR) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, mode)
    return target


def ensure_private_file(path: str | os.PathLike[str], mode: int = PRIVATE_FILE) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_RDWR, mode)
    os.close(fd)
    os.chmod(target, mode)
    return target


def secure_write(path: str | os.PathLike[str], data: str, mode: int = PRIVATE_FILE) -> Path:
    target = Path(path)
    ensure_private_dir(target.parent)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, target)
    return target


def _mode_issue(path: Path, actual: int, expected: int) -> str | None:
    actual &= 0o777
    if actual == expected:
        return None
    return f"{path}: mode {actual:04o}, expected {expected:04o}"


def audit_path(path: str | os.PathLike[str], expected_mode: int) -> list[str]:
    target = Path(path)
    if not target.exists():
        return [f"{target}: missing"]
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode):
        return [f"{target}: symlink is not allowed in private state"]
    issue = _mode_issue(target, info.st_mode, expected_mode)
    return [issue] if issue else []


def audit_tree(
    root: str | os.PathLike[str],
    *,
    dir_mode: int = PRIVATE_DIR,
    file_mode: int = PRIVATE_FILE,
    skip_names: set[str] | None = None,
) -> list[str]:
    skip_names = skip_names or set()
    base = Path(root)
    if not base.exists():
        return [f"{base}: missing"]
    issues: list[str] = []
    for current, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skip_names]
        cur = Path(current)
        if cur != base:
            issues.extend(audit_path(cur, dir_mode))
        for name in files:
            if name in skip_names:
                continue
            issues.extend(audit_path(cur / name, file_mode))
    if not base.is_dir():
        issues.extend(audit_path(base, dir_mode))
    return issues


def enforce_tree(
    root: str | os.PathLike[str],
    *,
    dir_mode: int = PRIVATE_DIR,
    file_mode: int = PRIVATE_FILE,
) -> None:
    base = Path(root)
    if not base.exists():
        return
    os.chmod(base, dir_mode)
    for current, dirs, files in os.walk(base):
        cur = Path(current)
        for name in dirs:
            os.chmod(cur / name, dir_mode)
        for name in files:
            path = cur / name
            if path.is_symlink():
                continue
            os.chmod(path, file_mode)


def audit_accounts(
    accounts_root: str | os.PathLike[str],
    *,
    expected_uid: int | None = None,
) -> list[str]:
    """Audit account config/data directories and credential/session files.

    Expected layout per account: 0700 for the account/config/data directories,
    0600 for every file (auth.json, realm.json, session.json, agent-wow.yaml).
    """
    base = Path(accounts_root)
    if not base.is_dir():
        return [f"{base}: missing"]
    issues: list[str] = []
    for account in sorted(p for p in base.iterdir() if p.is_dir()):
        issues.extend(audit_path(account, PRIVATE_DIR))
        for name in ("config", "data"):
            child = account / name
            if child.exists():
                issues.extend(audit_path(child, PRIVATE_DIR))
        for current, _dirs, files in os.walk(account):
            for name in files:
                path = Path(current) / name
                issues.extend(audit_path(path, PRIVATE_FILE))
        if expected_uid is not None:
            try:
                uid = account.stat().st_uid
            except OSError:
                uid = -1
            if uid != expected_uid:
                issues.append(f"{account}: uid {uid}, expected {expected_uid}")
    return issues


def ownership_plan(
    data_root: str | os.PathLike[str],
    *,
    growth_uid: int,
    coordinator_uid: int,
    operator_uid: int,
) -> list[tuple[Path, int, int]]:
    """Structural ownership model for the growth runtime tree.

    - ``state/``   -> growth worker UID, 0700
    - ``coordinator/`` -> coordinator/validator UID, 0700
    - ``control/`` -> operator UID, 0711 (traversable, not writable by growth);
      the STOP file inside is 0600 operator-owned.
    - the data root itself is operator-owned and traversable (0711) so a
      separate growth UID can reach ``state/`` without listing the tree.
    """
    root = Path(data_root)
    return [
        (root, operator_uid, 0o711),
        (root / "state", growth_uid, 0o700),
        (root / "coordinator", coordinator_uid, 0o700),
        (root / "control", operator_uid, 0o711),
    ]
