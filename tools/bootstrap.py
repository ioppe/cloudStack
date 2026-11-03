#!/usr/bin/env python3
"""Generate a private controller config and delivery credentials; never prints password."""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import hash_password


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--credentials", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    output, credentials = Path(args.output), Path(args.credentials)
    if output.exists() or credentials.exists():
        raise SystemExit("Refusing to overwrite an existing configuration or credentials file")
    config = json.loads((Path(__file__).resolve().parents[1] / "config/controller.example.json").read_text())
    password = secrets.token_urlsafe(20)
    config["password_hash"] = hash_password(password)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    os.chmod(str(output), 0o600)
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("云栈平台管理员凭据\n入口：http://192.168.0.30:8089/\n用户名：admin\n密码：" + password + "\n此文件仅供管理员保管，不随源码归档或提交。可通过平台密码接口更改；更改会使全部会话失效。\n")
    os.chmod(str(credentials), 0o600)
    print("Private config and credentials created (0600)")

if __name__ == "__main__":
    main()
