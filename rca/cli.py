"""Command line entry point: python -m rca.cli --metric conversion_rate"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from rca.engine import run_rca
from rca.report import to_markdown, to_summary
from rca.semantic_client import SemanticLayer

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metric", default="conversion_rate")
    ap.add_argument("--current-days", type=int, default=7)
    ap.add_argument("--baseline-days", type=int, default=28)
    ap.add_argument("--current-end", type=dt.date.fromisoformat, help="YYYY-MM-DD; default = latest day")
    ap.add_argument("--depth", type=int, choices=[1, 2], default=2)
    ap.add_argument("--out", type=Path, help="write the Markdown report here")
    ap.add_argument("--json", action="store_true", help="print a JSON summary instead of Markdown")
    ap.add_argument("--project-dir", type=Path, default=ROOT)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    sl = SemanticLayer(args.project_dir)
    result = run_rca(sl, args.metric, args.current_days, args.baseline_days, args.current_end, args.depth)

    md = to_markdown(result)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
    if args.json:
        print(json.dumps(to_summary(result), indent=2))
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
