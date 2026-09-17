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
    """Read device objects or merged arrays; reject conflicting duplicates."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    records, seen = [], {}
    for path in sorted(map(Path, paths)):
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


def download_dataset(email, api_key, output_root="downloads", group_ids=None):
    """Create one isolated snapshot. Publish the latest pointer only after success."""
    output_root = Path(output_root)
    with requests.Session() as session:
        groups = search_groups(session, email, api_key)
        if group_ids is not None:
            selected = {str(gid) for gid in group_ids}
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

        def refresh(gid):
            fresh = search_groups(session, email, api_key)
            match = next((g for g in fresh if str(g["id"]) == gid), None)
            if match is None:
                raise DownloadError(f"Group {gid} is no longer available to this account.")
            return match

        for group in groups:
            gid = str(group["id"])
            print(f"Downloading group {gid} ({group.get('name', '')})...")
            archive = download_archive(session, group, run_dir / f"{gid}.zip", refresh)
            folder = extract_archive(archive, run_dir / gid)
            paths = sorted(folder.rglob("*.json"))
            if not paths:
                raise DownloadError(f"Group {gid}: the archive contains no device JSON files.")
            json_paths.extend(paths)
        records = load_records(json_paths)
        if not records:
            raise DownloadError("The downloaded archives contain no device records.")
        records_file = write_records(records, run_dir / "records.json")
        # This local pointer contains no API key or signed URLs.
        write_records({"records_file": f"{run_dir.name}/records.json"}, output_root / "latest.json")
        print(f"Saved {len(records)} devices to {records_file}")
        return records_file


def latest_records(output_root="downloads"):
    root = Path(output_root)
    pointer = root / "latest.json"
    if not pointer.is_file():
        raise FileNotFoundError("Run 'python solarcell.py download' first, or select an existing JSON file.")
    data = json.loads(pointer.read_text(encoding="utf-8"))
    path = root / data["records_file"]
    if not path.is_file():
        raise FileNotFoundError("The latest download is missing. Select an existing JSON file or download again.")
    return path


def measurement_types(record):
    """Use exact case-insensitive keys: PL must not also match TRPL."""
    result = set()
    for field in ("analysis", "analysisInfo"):
        data = record.get(field)
        if isinstance(data, dict):
            result.update(str(key).upper() for key, value in data.items() if value)
    if record.get("JV"):  # previous merge_json_recipes_JV.json format
        result.add("JV")
    return sorted(result)


def filter_records(records, measurement=None):
    if measurement is None:
        return list(records)
    measurement = measurement.strip().upper()
    if not measurement:
        raise ValueError("Set measurement to a key such as SEM, or use None for all records.")
    return [record for record in records if measurement in measurement_types(record)]


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
        return [jv]
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


def export_csv(records, output_dir="exports"):
    """Export a new directory; keep previous exports intact, including on empty input."""
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
    return {"devices": len(devices), "jv_measurements": len(measurements), "output_dir": output_dir}
