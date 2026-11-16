"""Trusted local administration; never expose these operations as HTTP routes."""
import argparse
import getpass
import os
from pathlib import Path

from app.composition import build_container
from app.domain.errors import DomainError


def _main():
    parser = argparse.ArgumentParser(description="科研助手本地管理员工具，需要数据库文件访问权")
    parser.add_argument("command", choices=["bootstrap", "reset-password"])
    parser.add_argument("--db", type=Path, default=Path(os.getenv("RESEARCH_DATA_DIR", "research-data")) / "research.db")
    parser.add_argument("--email", required=True)
    parser.add_argument("--project")
    args = parser.parse_args()
    if args.command == "bootstrap" and not args.project:
        parser.error("bootstrap requires --project")
    password = getpass.getpass("New password (minimum 12 characters): ")
    if password != getpass.getpass("Confirm password: "):
        parser.error("Passwords do not match")
    try:
        service = build_container(args.db).service
        if args.command == "bootstrap":
            result = service.bootstrap(args.email, password, args.project)
            print(f"Initialized project {result['project']['id']}; sign in using the owner account.")
        else:
            service.recover_password(args.email, password)
            print("Password reset; all existing account sessions have been revoked.")
    except DomainError as error:
        parser.exit(1, f"{error.message}\n")


if __name__ == "__main__":
    _main()
