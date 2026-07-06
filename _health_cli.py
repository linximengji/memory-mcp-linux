#!/usr/bin/env python
"""One-shot CLI entry point for memory health check.

Usage:
    python _health_cli.py                          # L1 only, JSON output
    python _health_cli.py --llm                    # L1 + L3, JSON output
    python _health_cli.py --format html            # L1 only, HTML output
    python _health_cli.py --llm --format html      # L1 + L3, HTML output
"""
import json, sys, os

# Allow running from any CWD
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _health import run_memory_health, format_html


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Memory health check")
    parser.add_argument("--llm", action="store_true",
                        help="enable L3 deep audit with LLM")
    parser.add_argument("--format", choices=["json", "html"], default="json",
                        help="output format (default: json)")
    args = parser.parse_args()

    result = run_memory_health(llm_enabled=args.llm)

    if args.format == "html":
        if "error" in result:
            print(result["error"], file=sys.stderr)
            sys.exit(1)
        html = format_html(result)
        print(html)
    else:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        print()

    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
