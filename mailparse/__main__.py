"""命令行入口：python3 -m mailparse message.eml [--with-text]"""

from __future__ import annotations

import argparse
import json
import sys

from .parser import parse_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析 MIME 邮件报文并输出 JSON 结构")
    parser.add_argument("eml", help=".eml 报文文件路径")
    parser.add_argument("--with-text", action="store_true", help="在输出中包含解码后的正文文本")
    args = parser.parse_args(argv)

    root = parse_file(args.eml)
    json.dump(root.to_dict(include_text=args.with_text), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
