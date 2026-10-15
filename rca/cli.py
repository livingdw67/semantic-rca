"""Command line entry point: python -m rca.cli --metric conversion_rate"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from rca.crawl import CrawlConfig
from rca.pipeline import PipelineConfig, run_rca
from rca.semantic_client import SemanticLayer

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metric", default="conversion_rate")
    ap.add_argument("--current-end", type=dt.date.fromisoformat, help="YYYY-MM-DD; default = latest day")
    ap.add_argument("--current-days", type=int, default=7)
    ap.add_argument("--baseline-days", type=int, default=28)
    ap.add_argument("--mode", choices=["staged", "exhaustive"], default="staged",
                    help="staged pipeline (default) or the legacy exhaustive oracle")
    ap.add_argument("--no-forecast", action="store_true", help="naive baseline window instead of the forecast")
    ap.add_argument("--screen-sample", type=float, help="successive halving on a hash sample, e.g. 0.1")
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--allow-partial", action="store_true",
                    help="keep trailing days that look incomplete (pre-flight drops them by default)")
    ap.add_argument("--out", type=Path, help="write the Markdown report here")
    ap.add_argument("--json", action="store_true", help="print a JSON summary instead of Markdown")
    ap.add_argument("--project-dir", type=Path, default=ROOT)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    sys.stdout.reconfigure(encoding="utf-8")
    sl = SemanticLayer(args.project_dir)

    if args.mode == "exhaustive":
        from rca.exhaustive import run_rca as run_exhaustive
        from rca.exhaustive_report import to_markdown, to_summary

        result = run_exhaustive(sl, args.metric, args.current_days, args.baseline_days, args.current_end)
    else:
        from rca.report import to_markdown, to_summary

        crawl = dataclasses.replace(CrawlConfig(), screen_sample=args.screen_sample, max_depth=args.max_depth)
        config = dataclasses.replace(
            PipelineConfig(), current_days=args.current_days, baseline_days=args.baseline_days,
            use_forecast=not args.no_forecast, crawl=crawl, allow_partial=args.allow_partial,
        )
        result = run_rca(sl, args.metric, current_end=args.current_end, config=config)

    md = to_markdown(result)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
    print(json.dumps(to_summary(result), indent=2, default=str) if args.json else md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
