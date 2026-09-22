"""Shared download and CSV helpers for the Solar.ChemDX Python examples."""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
import stat
import tempfile
import zipfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import requests


API_URL = "https://solar.chemdx.org/api/v1/search"
TIMEOUT = (15, 120)  # connect, read seconds


class DownloadError(RuntimeError):
    """A download failure with no credentials or signed URLs in its message."""


def _network_error(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        return DownloadError(
            "TLS verification failed. Set REQUESTS_CA_BUNDLE to your organisation's "
            "CA certificate file if your network uses a TLS proxy."
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return DownloadError("The request timed out. Check your connection and retry.")
    return DownloadError("The request failed. Check your network connection and retry.")


def search_groups(session, email, api_key):
    """Return validated group metadata. The server may report auth failure as HTTP 200."""
    if not email.strip() or not api_key.strip():
        raise ValueError("An account email and API key are required.")
    try:
        with session.post(
            API_URL, json={"email": email.strip(), "api_key": api_key.strip()},
            timeout=TIMEOUT,
        ) as response:
            if response.status_code in (401, 403):
                raise DownloadError("Authentication failed. Check your account and API key.")
            if not response.ok:
                raise DownloadError(f"API returned HTTP {response.status_code}. Retry later.")
            try:
                groups = response.json()
            except ValueError:
                raise DownloadError("API returned invalid JSON.") from None
    except requests.RequestException as exc:
        raise _network_error(exc) from None
    if isinstance(groups, dict) and groups.get("status") == "failed":
        raise DownloadError("Authentication failed. Check your account and API key.")
    if not isinstance(groups, list):
        raise DownloadError("Expected a list of groups from the API.")
    seen = set()
    for group in groups:
        if not isinstance(group, dict) or not re.fullmatch(r"[0-9]+", str(group.get("id", ""))):
            raise DownloadError("API returned an invalid group ID.")
        gid = str(group["id"])
        if gid in seen:
            raise DownloadError("API returned duplicate group IDs.")
        seen.add(gid)
        url = group.get("temporaryUrl")
        try:
            parts = urlsplit(url) if isinstance(url, str) else None
            valid = parts and parts.scheme == "https" and parts.hostname and not parts.username
        except ValueError:
            valid = False
        if not valid:
            raise DownloadError(f"Group {gid} has no valid HTTPS download URL.")
    return groups


def download_archive(session, group, destination, refresh_group):
    """Stream a ZIP and refresh an expired signed URL once on HTTP 403."""
    destination = Path(destination)
    partial = destination.with_suffix(".zip.part")
    if destination.exists() or partial.exists():
        raise FileExistsError(f"Download destination already exists: {destination}")
    try:
        for attempt in range(2):
            with session.get(group["temporaryUrl"], stream=True, timeout=TIMEOUT) as response:
                if response.status_code == 403 and attempt == 0:
                    group = refresh_group(str(group["id"]))
                    continue
                if not response.ok:
                    raise DownloadError(
                        f"Group {group['id']}: download returned HTTP {response.status_code}. "
                        "Run the download again to request fresh links."
                    )
                with partial.open("xb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
                partial.replace(destination)
                return destination
    except requests.RequestException as exc:
        raise _network_error(exc) from None
    finally:
        partial.unlink(missing_ok=True)


def extract_archive(archive, destination):
    """Extract into a new directory, rejecting unsafe or ambiguous ZIP paths."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"Extraction directory already exists: {destination}")
    with zipfile.ZipFile(archive) as zipped:
        members = []
        seen = set()
        for member in zipped.infolist():
            name = member.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            mode = member.external_attr >> 16
            if (not parts or name.startswith("/") or ".." in parts
                    or any(":" in part or part.endswith((" ", ".")) for part in parts)
                    or any(part.split(".")[0].upper() in {
                        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                        *(f"LPT{i}" for i in range(1, 10)),
                    } for part in parts)
                    or stat.S_ISLNK(mode)):
                raise ValueError("Archive contains an unsafe file path or symbolic link.")
            key = "/".join(parts).casefold()
            if key in seen:
                raise ValueError("Archive contains duplicate file paths.")
            seen.add(key)
            members.append((member, destination.joinpath(*parts)))
        destination.mkdir(parents=True)
        for member, target in members:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    return destination


def load_records(paths):
    """Read JSON files or a run/group folder; reject conflicting duplicates.

    Prefer a run's records.json when present. Otherwise read its numeric group
    folders, excluding optional filtered output stored beside those folders.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    json_paths = []
    for path in map(Path, paths):
        if not path.is_dir():
            json_paths.append(path)
            continue
        if (path / "latest.json").is_file():
            raise ValueError("Select a run or group folder, not the downloads root.")
        merged = path / "records.json"
        if merged.is_file():
            json_paths.append(merged)
            continue
        group_folders = sorted(
            child for child in path.iterdir()
            if child.is_dir() and re.fullmatch(r"[0-9]+", child.name)
        )
        if group_folders:
            found = [item for folder in group_folders for item in folder.rglob("*.json")]
        else:
            found = [item for item in path.rglob("*.json")
                     if item.name not in {"latest.json", "filtered_records.json"}]
        if not found:
            raise ValueError(f"No device JSON files found in folder: {path}")
        json_paths.extend(found)
    records, seen = [], {}
    for path in sorted(json_paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError) as exc:
            raise ValueError(f"Cannot read device JSON: {path.name}") from exc
        items = data if isinstance(data, list) else [data]
        for record in items:
            if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
                    or not record["id"].strip()):
                raise ValueError(f"Expected a device object with a non-empty string ID: {path.name}")
            identity = (str(record.get("group", "")), record["id"])
            if identity in seen:
                if record != seen[identity]:
                    raise ValueError(f"Conflicting duplicate device: {record['id']}")
                continue
            seen[identity] = record
            records.append(record)
    return records


def write_records(records, path):
    """Write JSON atomically; used for full records and the completed-run pointer."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as output:
        temporary = Path(output.name)
        try:
            json.dump(records, output, ensure_ascii=False)
        except BaseException:
            output.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def is_all_selection(value):
    """Recognize explicit ALL selections and the legacy None spelling."""
    return value is None or (isinstance(value, str) and value.strip().upper() == "ALL")


def normalize_measurements(measurement):
    """Return uppercase keys, or None for ALL; accept a string or key collection."""
    if is_all_selection(measurement):
        return None
    message = "Use 'ALL', one measurement key, or a non-empty list such as ['JV', 'PL']."
    if isinstance(measurement, str):
        values = [measurement]
    elif isinstance(measurement, (list, tuple, set, frozenset)):
        values = measurement
    else:
        raise ValueError(message)
    keys = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(message)
        for key in value.split(","):
            key = key.strip().upper()
            if not key:
                raise ValueError(message)
            keys.add(key)
    if not keys:
        raise ValueError(message)
    if "ALL" in keys:
        if len(keys) != 1:
            raise ValueError("Use 'ALL' alone, or select specific measurement keys; do not combine them.")
        return None
    return frozenset(keys)


def _write_selected_group(source_folder, paths, destination, measurements):
    """Save selected device JSON with the archive's relative paths and containers."""
    written = False
    for path in paths:
        # These source files were already validated together by load_records().
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        records = data if isinstance(data, list) else [data]
        selected = filter_records(records, measurements)
        if selected:
            write_records(selected if isinstance(data, list) else selected[0],
                          destination / path.relative_to(source_folder))
            written = True
    if not written:
        # An explicit empty group remains readable by the CSV workflow.
        write_records([], destination / "records.json")


def download_dataset(email, api_key, output_root="downloads", group_ids="ALL", save_mode="both",
                     measurement="ALL"):
    """Create a snapshot, publishing the latest pointer only after success.

    both: save group JSON and records.json (the default).
    grouped: save group JSON without a merged file at the run root.
    merged: save only records.json.

    Select devices with any requested measurement and retain only those
    measurement fields plus device/recipe metadata. ALL retains full records.
    Original ZIPs are kept only for ALL with both/grouped; otherwise the original
    downloads are temporary. No additional filtered_records.json is created.

    Return records.json for both/merged, or the run folder for grouped. All
    returned paths can be passed directly to load_records().
    group_ids="ALL" selects every available group; None remains compatible.
    """
    if save_mode not in ("both", "grouped", "merged"):
        raise ValueError("save_mode must be 'both', 'grouped', or 'merged'.")
    measurements = normalize_measurements(measurement)
    selected = None
    if not is_all_selection(group_ids):
        if isinstance(group_ids, str):
            raise ValueError("Set group_ids to 'ALL' or a list of group IDs such as [2, 3].")
        selected = {str(gid).strip() for gid in group_ids}
        if any(gid.upper() == "ALL" for gid in selected):
            if len(selected) != 1:
                raise ValueError("Use 'ALL' alone, or select specific group IDs; do not combine them.")
            selected = None
    output_root = Path(output_root)
    with requests.Session() as session:
        groups = search_groups(session, email, api_key)
        if selected is not None:
            available = {str(group["id"]) for group in groups}
            if selected - available:
                raise ValueError("Some selected groups are absent from this account's API response.")
            groups = [group for group in groups if str(group["id"]) in selected]
        if not groups:
            raise DownloadError("No downloadable groups were returned or selected.")
        output_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = Path(tempfile.mkdtemp(prefix=f"run-{stamp}-", dir=output_root))
        json_paths = []
        group_sources = []

        def refresh(gid):
            fresh = search_groups(session, email, api_key)
            match = next((g for g in fresh if str(g["id"]) == gid), None)
            if match is None:
                raise DownloadError(f"Group {gid} is no longer available to this account.")
            return match

        with ExitStack() as temporary_files:
            working_dir = run_dir
            if save_mode == "merged" or measurements is not None:
                working_dir = Path(temporary_files.enter_context(
                    tempfile.TemporaryDirectory(prefix=".download-", dir=run_dir)
                ))
            for group in groups:
                gid = str(group["id"])
                print(f"Downloading group {gid} ({group.get('name', '')})...")
                archive = download_archive(session, group, working_dir / f"{gid}.zip", refresh)
                folder = extract_archive(archive, working_dir / gid)
                paths = sorted(folder.rglob("*.json"))
                if not paths:
                    raise DownloadError(f"Group {gid}: the archive contains no device JSON files.")
                json_paths.extend(paths)
                group_sources.append((gid, folder, paths))
            # Validate every mode before publishing, including grouped-only runs.
            records = load_records(json_paths)
            if not records:
                raise DownloadError("The downloaded archives contain no device records.")
            total = len(records)
            records = filter_records(records, measurements)
            if measurements is not None and save_mode != "merged":
                for gid, folder, paths in group_sources:
                    _write_selected_group(folder, paths, run_dir / gid, measurements)
            if save_mode == "grouped":
                records_source = run_dir
                pointer = {"records_dir": run_dir.name}
            else:
                records_source = write_records(records, run_dir / "records.json")
                pointer = {"records_file": f"{run_dir.name}/records.json"}
        # This local pointer contains no API key or signed URLs.
        write_records(pointer, output_root / "latest.json")
        print(f"Saved {len(records)} of {total} devices to {records_source} (save_mode={save_mode})")
        return records_source


def latest_records(output_root="downloads"):
    """Return the latest complete JSON file or grouped run folder."""
    root = Path(output_root)
    pointer = root / "latest.json"
    if not pointer.is_file():
        raise FileNotFoundError("Run 'python solarcell.py download' first, or select an existing JSON file.")
    message = "Invalid latest.json. Run 'python solarcell.py download' again, or select an existing JSON file."
    try:
        data = json.loads(pointer.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ValueError(message) from None
    keys = [key for key in ("records_file", "records_dir") if isinstance(data, dict) and key in data]
    if len(keys) != 1:
        raise ValueError(message)
    source_key = keys[0]
    relative = data[source_key]
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError(message)
    relative = Path(relative)
    if not relative.parts or relative.anchor or ".." in relative.parts:
        raise ValueError(message)
    path = root / relative
    exists = path.is_dir() if source_key == "records_dir" else path.is_file()
    if not exists:
        raise FileNotFoundError("The latest download is missing. Select an existing JSON file or download again.")
    return path


def measurement_types(record):
    """Use exact case-insensitive keys: PL must not also match TRPL."""
    result = set()
    for field in ("analysis", "analysisInfo"):
        data = record.get(field)
        if isinstance(data, dict):
            result.update(str(key).strip().upper() for key, value in data.items() if value)
    if record.get("JV"):  # previous merge_json_recipes_JV.json format
        result.add("JV")
    return sorted(result)


def filter_records(records, measurement="ALL"):
    """Keep any matching device and only selected measurements, preserving metadata.

    Case-insensitive exact matching applies to analysis, analysisInfo and legacy
    top-level JV. Source records are not modified. ALL preserves every field.
    """
    measurements = normalize_measurements(measurement)
    if measurements is None:
        return list(records)
    result = []
    for record in records:
        if not measurements.intersection(measurement_types(record)):
            continue
        selected = dict(record)
        for field in ("analysis", "analysisInfo"):
            if field in record:
                data = record[field]
                selected[field] = {key: value for key, value in data.items()
                                   if str(key).strip().upper() in measurements} if isinstance(data, dict) else {}
        if "JV" not in measurements:
            selected.pop("JV", None)
        result.append(selected)
    return result


def _flatten(value, prefix, output, expand_lists=False):
    # Escape separators in source keys so distinct JSON paths cannot overwrite each other.
    if isinstance(value, dict) and value:
        for key, item in value.items():
            key = str(key).replace("~", "~0").replace(".", "~1")
            _flatten(item, f"{prefix}.{key}", output, expand_lists)
    elif expand_lists and isinstance(value, list) and value:
        for index, item in enumerate(value):
            _flatten(item, f"{prefix}.{index}", output, expand_lists)
    else:
        output[prefix] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value


def _scalar(value):
    return value["value"] if isinstance(value, dict) and "value" in value else value


def _number(value):
    value = _scalar(value)
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _jv_entries(record):
    analysis = record.get("analysis")
    jv = analysis.get("JV") if isinstance(analysis, dict) else None
    if jv is None:
        jv = record.get("JV", [])
    if isinstance(jv, dict):
        return [jv] if jv else []
    if isinstance(jv, list):
        return jv
    return [] if jv is None else [jv]


def _jv_metrics(entry):
    if not isinstance(entry, dict):
        return {}
    parameters = entry.get("pv_cell_parameters")
    parameters = parameters if isinstance(parameters, dict) else entry
    return {key: _number(parameters.get(key)) for key in ("efficiency", "fill_factor", "jsc", "voc")}


def csv_rows(records):
    """One device row including all recipe fields, and one row per JV entry."""
    devices, measurements = [], []
    for record in records:
        row = {"id": record["id"], "group": record.get("group"),
               "author": _scalar(record.get("author")),
               "measurement_types": ";".join(measurement_types(record))}
        recipe = record.get("input")
        _flatten(recipe, "input", row, expand_lists=True)
        steps = recipe if isinstance(recipe, list) else [recipe]
        fields = {key: _scalar(value) for step in steps if isinstance(step, dict)
                  for key, value in step.items()}
        for column, key in {
            "TCO": "tco", "AND": "anode_metal", "device_type": "device_type", "note": "note",
            "temperature": "temperature", "humidity": "humidity", "cell_dimension": "cell_dimension",
        }.items():
            row[column] = fields.get(key)
        entries = _jv_entries(record)
        row["JV_count"] = len(entries)
        best = None
        for index, entry in enumerate(entries):
            metrics = _jv_metrics(entry)
            measurement = {"id": record["id"], "group": record.get("group"),
                           "jv_index": index, **metrics}
            _flatten(entry, "JV", measurement)
            measurements.append(measurement)
            efficiency = metrics.get("efficiency")
            if efficiency is not None and (best is None or efficiency > best[1]["efficiency"]):
                best = (index, metrics)
        row["best_jv_index"] = best[0] if best else None
        for column, key in {"Ef_max": "efficiency", "FF_max": "fill_factor",
                            "JS_max": "jsc", "V0_max": "voc"}.items():
            row[column] = best[1].get(key) if best else None
        devices.append(row)
    return devices, measurements


def _write_csv(rows, path, first_columns):
    columns = list(first_columns) + sorted({key for row in rows for key in row} - set(first_columns))
    with Path(path).open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False)
                             if isinstance(value, (list, dict)) else value for key, value in row.items()})


def _write_measurements_csv(records, path):
    """Write one device/type row, preserving measurement data and attachment metadata."""
    columns = ["id", "group", "measurement_type", "analysis_json", "analysis_info_json", "legacy_jv_json"]
    count = 0
    with Path(path).open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for record in records:
            for kind in measurement_types(record):
                row = {"id": record["id"], "group": record.get("group"), "measurement_type": kind}
                for field, column in (("analysis", "analysis_json"), ("analysisInfo", "analysis_info_json")):
                    data = record.get(field)
                    selected = {key: value for key, value in data.items()
                                if str(key).strip().upper() == kind} if isinstance(data, dict) else {}
                    row[column] = json.dumps(selected, ensure_ascii=False) if selected else ""
                if kind == "JV" and "JV" in record:
                    row["legacy_jv_json"] = json.dumps(record["JV"], ensure_ascii=False)
                writer.writerow(row)
                count += 1
    return count


def export_csv(records, output_dir="exports"):
    """Write devices.csv, jv.csv and measurements.csv into a new output directory."""
    records = list(records)
    output_dir = Path(output_dir)
    devices, measurements = csv_rows(records)
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_csv(devices, output_dir / "devices.csv", [
        "id", "group", "author", "measurement_types", "TCO", "AND", "device_type", "note",
        "JV_count", "best_jv_index", "Ef_max", "FF_max", "JS_max", "V0_max",
    ])
    _write_csv(measurements, output_dir / "jv.csv", [
        "id", "group", "jv_index", "efficiency", "fill_factor", "jsc", "voc",
    ])
    measurement_rows = _write_measurements_csv(records, output_dir / "measurements.csv")
    return {"devices": len(devices), "jv_measurements": len(measurements),
            "measurement_rows": measurement_rows, "output_dir": output_dir}
