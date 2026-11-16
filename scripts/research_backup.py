"""Consistent SQLite backup; archives contain private research data. Keep them private."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser(description="备份科研数据库（含账号和私有资料，须妥善保管）")
    parser.add_argument("--db", type=Path, default=Path(os.getenv("RESEARCH_DATA_DIR", "research-data")) / "research.db")
    parser.add_argument("--output-dir", type=Path, default=Path("research-backups"))
    args = parser.parse_args()
    source = args.db.resolve(strict=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / f"research-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}.db"
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as incoming, sqlite3.connect(target) as outgoing:
        incoming.backup(outgoing)
        if outgoing.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份完整性检查失败")
    print(f"已生成一致性备份：{target}。包含私有资料，请限制访问并配置加密离线副本。")


if __name__ == "__main__":
    main()
