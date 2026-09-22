"""Command-line examples: python solarcell.py download / csv."""

import argparse
import os
import sys
import zipfile
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from uuid import uuid4

from solarcell_tools import (
    DownloadError,
    download_dataset,
    export_csv,
    filter_records,
    is_all_selection,
    latest_records,
    load_records,
    normalize_measurements,
)


def _group_id(value):
    if is_all_selection(value):
        return "ALL"
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use ALL or integer group IDs.") from None


def parser():
    result = argparse.ArgumentParser(description="Download Solar.ChemDX device data and export CSV.")
    commands = result.add_subparsers(dest="command", required=True)

    download = commands.add_parser("download", help="Download archives available to your account")
    download.add_argument("--email", help="Account email (default: SOLARCELL_EMAIL or a prompt)")
    download.add_argument("--groups", nargs="+", type=_group_id, default="ALL", metavar="ID",
                          help="ALL or selected group IDs, e.g. 2 3 (default: ALL)")
    download.add_argument("--output-root", type=Path, default=Path("downloads"),
                          help="Download directory (default: downloads)")
    download.add_argument("--save-mode", choices=("both", "grouped", "merged"), default="both",
                          help="Keep both JSON layouts, group files only, or merged records.json only (default: both)")
    download.add_argument("--measurement", nargs="+", default="ALL", metavar="TYPE",
                          help="Save only these measurements, e.g. JV PL SEM, or ALL (default: ALL)")

    convert = commands.add_parser("csv", help="Convert device JSON to device, JV and measurement CSV tables")
    convert.add_argument("--input", type=Path,
                         help="Device JSON file, merged array, or run/group folder (default: latest download)")
    convert.add_argument("--download-root", type=Path, default=Path("downloads"),
                         help="Directory containing latest.json (default: downloads)")
    convert.add_argument("--measurement", nargs="+", default="ALL", metavar="TYPE",
                         help="Keep only these measurements, e.g. JV PL, or ALL (default: ALL)")
    convert.add_argument("--output", type=Path, help="New output directory (default: exports/export-<unique ID>)")
    return result


def _credentials(email):
    email = email or os.environ.get("SOLARCELL_EMAIL")
    api_key = os.environ.get("SOLARCELL_API_KEY")
    if (not email or not api_key) and not sys.stdin.isatty():
        raise ValueError("Set SOLARCELL_EMAIL and SOLARCELL_API_KEY for non-interactive downloads.")
    email = email or input("Solar.ChemDX account email: ").strip()
    api_key = api_key or getpass("Solar.ChemDX API key: ")
    return email, api_key


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        normalize_measurements(arguments.measurement)
        if arguments.command == "download":
            email, api_key = _credentials(arguments.email)
            try:
                path = download_dataset(email, api_key, arguments.output_root, arguments.groups,
                                        save_mode=arguments.save_mode, measurement=arguments.measurement)
            finally:
                del api_key
            print(f"CSV input: {path}")
        else:
            path = arguments.input if arguments.input is not None else latest_records(arguments.download_root)
            records = load_records(path)
            selected = filter_records(records, arguments.measurement)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output = arguments.output or Path("exports") / f"export-{stamp}-{uuid4().hex[:8]}"
            summary = export_csv(selected, output)
            print(f"Loaded {len(records)} devices; selected {len(selected)} from {path}")
            print(f"Saved {summary['devices']} devices, {summary['jv_measurements']} JV entries, "
                  f"and {summary['measurement_rows']} rows in measurements.csv to {output}")
        return 0
    except (DownloadError, ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except EOFError:
        print("Error: Set SOLARCELL_EMAIL and SOLARCELL_API_KEY for non-interactive downloads.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
