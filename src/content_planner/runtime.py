"""Runtime prerequisites, independent of model credentials or application state."""
import platform
import re
import sqlite3


def diagnose_runtime():
    python = platform.python_version()
    sqlite = sqlite3.sqlite_version
    py_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", python)
    sql_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", sqlite)
    py = tuple(map(int, py_match.groups())) if py_match else ()
    sql = tuple(map(int, sql_match.groups())) if sql_match else ()
    sqlite_fixed = bool(sql and sql[0] == 3 and
                        (sql >= (3, 51, 3) or sql[:2] == (3, 50) and sql[2] >= 7 or sql[:2] == (3, 44) and sql[2] >= 6))
    checks = {"python_supported_by_project": py[:2] == (3, 14), "sqlite_fix_level_confirmed": sqlite_fixed}
    return {"scope": "runtime_prerequisites_only; not a production-readiness certification",
            "runtime": {"python": python, "sqlite": sqlite}, "checks": checks,
            "release_ready": all(checks.values()), "journal_policy": "rollback journal; WAL is not enabled automatically",
            "note": "旧发行版的补丁状态需要额外证明；本检查不凭版本字符串认定数据已损坏。"}
