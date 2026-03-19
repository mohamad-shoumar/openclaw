from __future__ import annotations

import argparse
import os
from pathlib import Path

from .pipeline import run_pipeline


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenClaw strict-mode Phase 1 job pipeline")
    parser.add_argument(
        "command",
        choices=["run"],
        help="Pipeline command to execute.",
    )
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="Workspace root containing config, data, and outputs directories.",
    )
    parser.add_argument(
        "--config-dir",
        default="config",
        help="Configuration directory relative to the workspace root.",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Data directory relative to the workspace root.",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Output directory relative to the workspace root.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root).resolve()
    load_dotenv(workspace_root / ".env")
    config_dir = workspace_root / args.config_dir
    data_dir = workspace_root / args.data_dir
    output_dir = workspace_root / args.output_dir

    if args.command == "run":
        summary = run_pipeline(
            workspace_root=workspace_root,
            config_dir=config_dir,
            data_dir=data_dir,
            output_dir=output_dir,
        )
        print(summary.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
