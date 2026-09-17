import csv
import importlib.util
import io
import json
import os
import tempfile
import subprocess
import sys
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests

import solarcell_tools as tools
import solarcell as cli


def archive_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


class Response:
    def __init__(self, data=None, status=200, content=b"", error=None):
        self.data, self.status_code, self.content, self.error = data, status, content, error
        self.ok = status < 400

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def iter_content(self, chunk_size):
        yield self.content
        if self.error:
            raise self.error


class Session:
    def __init__(self, posts=(), gets=()):
        self.posts, self.gets = list(posts), list(gets)
        self.post_calls, self.get_calls = [], []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        result = self.posts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.gets.pop(0)


def group(url="https://example.org/archive.zip?signature=secret"):
    return {"id": 2, "name": "Example group", "temporaryUrl": url}


class DownloadTests(unittest.TestCase):
    def test_http_200_authentication_failure(self):
        session = Session(posts=[Response({"status": "failed", "message": "Invalid user"})])
        with self.assertRaisesRegex(tools.DownloadError, "Authentication failed"):
            tools.search_groups(session, "user@example.org", "secret")

    def test_bad_api_shapes_and_paths(self):
        for value in ({}, "html", [{"id": "../2"}], [group("http://example.org/zip")],
                      [group(), group()], ValueError("not JSON")):
            with self.subTest(value=value), self.assertRaises(tools.DownloadError):
                tools.search_groups(Session(posts=[Response(value)]), "email", "key")

    def test_timeout_does_not_reveal_signed_urls(self):
        session = Session(posts=[requests.Timeout("URL with a secret")])
        with self.assertRaisesRegex(tools.DownloadError, "timed out") as error:
            tools.search_groups(session, "email", "key")
        self.assertNotIn("secret", str(error.exception))

    def test_tls_stays_verified(self):
        session = Session(posts=[Response([group()])])
        tools.search_groups(session, "email", "key")
        arguments = session.post_calls[0][1]
        self.assertIn("timeout", arguments)
        self.assertNotEqual(arguments.get("verify"), False)

    def test_expired_link_refreshes_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = []
            session = Session(gets=[Response(status=403), Response(content=b"archive")])
            def refresh(gid):
                calls.append(gid)
                return group("https://example.org/fresh.zip")
            path = tools.download_archive(session, group(), Path(temporary) / "2.zip", refresh)
            self.assertEqual(path.read_bytes(), b"archive")
            self.assertEqual(calls, ["2"])
            self.assertEqual(session.get_calls[-1][0], "https://example.org/fresh.zip")

    def test_repeated_403_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Session(gets=[Response(status=403), Response(status=403)])
            with self.assertRaisesRegex(tools.DownloadError, "HTTP 403"):
                tools.download_archive(session, group(), Path(temporary) / "2.zip", lambda gid: group())
            self.assertEqual(len(session.get_calls), 2)

    def test_partial_download_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Session(gets=[Response(content=b"partial", error=requests.ConnectionError("secret"))])
            path = Path(temporary) / "2.zip"
            with self.assertRaises(tools.DownloadError):
                tools.download_archive(session, group(), path, lambda gid: group())
            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix(".zip.part").exists())

    def test_zip_traversal_and_symlinks_are_rejected(self):
        for name in ("../escape.json", "/escape.json", "C:/escape.json", "..\\escape.json",
                     "a/../../escape.json", "NUL.json", "CON.extra.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "source.zip"
                source.write_bytes(archive_bytes({name: "{}"}))
                target = Path(temporary) / "records"
                with self.assertRaises(ValueError):
                    tools.extract_archive(source, target)
                self.assertFalse(target.exists())
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "symlink.zip"
            with zipfile.ZipFile(source, "w") as archive:
                info = zipfile.ZipInfo("link")
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                archive.writestr(info, "../outside")
            with self.assertRaises(ValueError):
                tools.extract_archive(source, Path(temporary) / "records")

    def test_nested_public_archive_and_snapshot_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = {"id": "SAMPLE-1", "input": [], "analysis": {"SEM": [{}]}}
            second = {"id": "SAMPLE-2", "input": []}
            for record in (first, second):
                zipped = archive_bytes({f"2-public/{record['id']}.json": json.dumps(record)})
                session = Session(posts=[Response([group()])], gets=[Response(content=zipped)])
                with patch.object(tools.requests, "Session", return_value=session):
                    output = tools.download_dataset("email", "key", root)
                self.assertEqual(tools.load_records(output), [record])
                self.assertEqual(tools.latest_records(root), output)
            self.assertEqual(len(list(root.glob("run-*"))), 2)
            latest = (root / "latest.json").read_text()
            self.assertNotIn("secret", latest)
            self.assertNotIn("temporaryUrl", latest)

    def test_failed_run_preserves_latest_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pointer = root / "latest.json"
            pointer.write_text('{"records_file":"previous/records.json"}')
            session = Session(posts=[Response([group()])], gets=[Response(status=500)])
            with patch.object(tools.requests, "Session", return_value=session):
                with self.assertRaises(tools.DownloadError):
                    tools.download_dataset("email", "key", root)
            self.assertEqual(json.loads(pointer.read_text())["records_file"], "previous/records.json")

    def test_empty_response_does_not_publish_a_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Session(posts=[Response([])])
            with patch.object(tools.requests, "Session", return_value=session):
                with self.assertRaisesRegex(tools.DownloadError, "No downloadable"):
                    tools.download_dataset("email", "key", temporary)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_only_selected_groups_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            groups = [group(), {**group(), "id": 3}]
            zipped = archive_bytes({"3/a.json": '{"id":"a"}'})
            session = Session(posts=[Response(groups)], gets=[Response(content=zipped)])
            with patch.object(tools.requests, "Session", return_value=session):
                output = tools.download_dataset("email", "key", temporary, group_ids=[3])
            self.assertEqual(len(session.get_calls), 1)
            self.assertTrue((output.parent / "3.zip").is_file())
            self.assertFalse((output.parent / "2.zip").exists())


class ConversionTests(unittest.TestCase):
    def test_measurement_filter_is_exact_and_handles_null(self):
        records = [{"id": "a", "analysis": {"TRPL": [1]}},
                   {"id": "b", "analysisInfo": {"PL": [1]}},
                   {"id": "c", "analysis": None, "analysisInfo": []},
                   {"id": "d", "analysis": {"PL": []}}]
        self.assertEqual([r["id"] for r in tools.filter_records(records, "pl")], ["b"])
        self.assertEqual(tools.filter_records(records), records)

    def test_duplicates_and_invalid_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "records.json"
            tools.write_records([{"id": "a"}, {"id": "a"}], path)
            self.assertEqual(tools.load_records(path), [{"id": "a"}])
            tools.write_records([{"id": "a", "input": []}, {"id": "a"}], path)
            with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
                tools.load_records(path)
            tools.write_records([{"input": []}], path)
            with self.assertRaisesRegex(ValueError, "device object"):
                tools.load_records(path)

    def test_no_jv_keeps_device_and_does_not_reuse_previous_fields(self):
        records = [{"id": "a", "input": [{"tco": {"value": "ITO"}, "note": {"value": "first"}}]},
                   {"id": "b", "input": None, "analysis": None}]
        devices, jv = tools.csv_rows(records)
        self.assertEqual(len(devices), 2)
        self.assertEqual(jv, [])
        self.assertEqual(devices[0]["TCO"], "ITO")
        self.assertIsNone(devices[1]["TCO"])
        self.assertIsNone(devices[1]["note"])
        self.assertIsNone(devices[1]["Ef_max"])

    def test_best_jv_uses_one_measurement_and_both_schemas(self):
        records = [{"id": "a", "analysis": {"JV": [
            {"efficiency": {"value": "15"}, "fill_factor": {"value": 80}},
            {"pv_cell_parameters": {"efficiency": {"value": "20"},
                                    "fill_factor": {"value": 70}, "voc": {"value": "1.1"}}},
            {"efficiency": {"value": "NaN"}, "fill_factor": {"value": 99}},
        ]}}]
        devices, jv = tools.csv_rows(records)
        self.assertEqual(len(jv), 3)
        self.assertEqual(devices[0]["best_jv_index"], 1)
        self.assertEqual(devices[0]["Ef_max"], 20)
        self.assertEqual(devices[0]["FF_max"], 70)
        self.assertEqual(devices[0]["V0_max"], 1.1)
        self.assertIsNone(devices[0]["JS_max"])

    def test_zero_units_and_repeated_recipe_values_survive(self):
        records = [{"id": "a", "input": [{"annealing": [
            {"temperature": {"value": 0, "unit": "C"}},
            {"temperature": {"value": "100", "unit": "C"}},
        ]}], "JV": [{"efficiency": {"value": 0}, "J": [0, 1]}]}]
        devices, jv = tools.csv_rows(records)
        self.assertEqual(devices[0]["input.0.annealing.0.temperature.value"], 0)
        self.assertEqual(devices[0]["input.0.annealing.1.temperature.unit"], "C")
        self.assertEqual(devices[0]["Ef_max"], 0)
        self.assertEqual(json.loads(jv[0]["JV.J"]), [0, 1])

    def test_csv_roundtrip_unicode_and_empty_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "csv"
            tools.export_csv([{"id": "a", "author": "연구자", "input": [{"note": {"value": "comma,\nline"}}]}], output)
            with (output / "devices.csv").open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(rows[0]["author"], "연구자")
            self.assertEqual(rows[0]["note"], "comma,\nline")
            self.assertNotIn("", rows[0])  # no pandas index column
            with self.assertRaises(FileExistsError):
                tools.export_csv([], output)
            empty = Path(temporary) / "empty"
            tools.export_csv([], empty)
            with (empty / "devices.csv").open(encoding="utf-8-sig") as source:
                reader = csv.DictReader(source)
                self.assertIn("id", reader.fieldnames)
                self.assertEqual(list(reader), [])


class CommandLineTests(unittest.TestCase):
    def test_download_and_csv_use_custom_root_and_exact_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = [{"id": "PL-1", "analysis": {"PL": [{}]}},
                       {"id": "TRPL-1", "analysis": {"TRPL": [{}]}}]
            zipped = archive_bytes({f"2-public/{r['id']}.json": json.dumps(r) for r in records})
            session = Session(posts=[Response([group()])], gets=[Response(content=zipped)])
            downloads = root / "custom-downloads"
            with patch.object(tools.requests, "Session", return_value=session), patch.dict(
                os.environ, {"SOLARCELL_EMAIL": "test@example.org", "SOLARCELL_API_KEY": "test-only"}
            ), redirect_stdout(io.StringIO()):
                result = cli.main(["download", "--output-root", str(downloads),
                                   "--groups", "2", "--measurement", "pl"])
            self.assertEqual(result, 0)
            latest = tools.latest_records(downloads)
            self.assertEqual(len(tools.load_records(latest)), 2)
            filtered = tools.load_records(latest.parent / "filtered_records.json")
            self.assertEqual([record["id"] for record in filtered], ["PL-1"])
            output = root / "csv"
            with redirect_stdout(io.StringIO()):
                result = cli.main(["csv", "--download-root", str(downloads),
                                   "--measurement", "PL", "--output", str(output)])
            self.assertEqual(result, 0)
            with (output / "devices.csv").open(encoding="utf-8-sig") as source:
                self.assertEqual([row["id"] for row in csv.DictReader(source)], ["PL-1"])

    def test_missing_input_returns_failure_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()) as stderr:
            result = cli.main(["csv", "--input", str(Path(temporary) / "missing.json")])
        self.assertEqual(result, 1)
        self.assertIn("Error:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_noninteractive_download_requires_credentials_before_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys.stdin, "isatty", return_value=False), \
                patch.object(cli, "download_dataset") as download, redirect_stderr(io.StringIO()) as stderr:
            result = cli.main(["download"])
        self.assertEqual(result, 1)
        self.assertIn("SOLARCELL_API_KEY", stderr.getvalue())
        download.assert_not_called()

    def test_terminal_csv_runs_without_notebook_packages(self):
        script = Path(__file__).resolve().parents[1] / "solarcell.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = tools.write_records([{"id": "SAMPLE", "JV": [{"efficiency": {"value": 0}}]}],
                                         root / "input.json")
            # A fresh interpreter must not need pandas, IPython, or Jupyter to run the documented command.
            runner = """
import runpy, sys
for name in ('pandas', 'IPython', 'jupyterlab', 'ipykernel'):
    sys.modules[name] = None
script = sys.argv[1]
sys.path.insert(0, str(__import__('pathlib').Path(script).parent))
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name='__main__')
"""
            output = root / "csv"
            command = [sys.executable, "-c", runner, str(script), "csv", "--input", str(source),
                       "--output", str(output)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            with (output / "devices.csv").open(encoding="utf-8-sig") as table:
                row = next(csv.DictReader(table))
                self.assertEqual(row["Ef_max"], "0.0")
            before = (output / "devices.csv").read_bytes()
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual((output / "devices.csv").read_bytes(), before)


@unittest.skipUnless(importlib.util.find_spec("pandas"), "Install requirements-notebooks.txt for notebook checks")
class NotebookTests(unittest.TestCase):
    def test_notebooks_run_in_order_without_network(self):
        root = Path(__file__).resolve().parents[1]
        examples = [
            {"id": "EXAMPLE-1", "input": [{"tco": {"value": "ITO"}}],
             "analysis": {"JV": [{"efficiency": {"value": "18.2"}}], "SEM": [{}]}},
            {"id": "EXAMPLE-2", "input": None},
        ]
        zipped = archive_bytes({f"2-public/{r['id']}.json": json.dumps(r) for r in examples})
        session = Session(posts=[Response([group()])], gets=[Response(content=zipped)])
        previous_directory = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                os.chdir(temporary)
                with patch.object(tools.requests, "Session", return_value=session), patch.dict(
                    os.environ, {"SOLARCELL_EMAIL": "test@example.org", "SOLARCELL_API_KEY": "test-only"}
                ):
                    for name in ("01_download_data.ipynb", "02_data_csv.ipynb"):
                        notebook = json.loads((root / name).read_text(encoding="utf-8"))
                        self.assertEqual(notebook["nbformat"], 4)
                        namespace = {}
                        for index, cell in enumerate(notebook["cells"]):
                            if cell["cell_type"] == "code":
                                self.assertIsNone(cell["execution_count"])
                                self.assertEqual(cell["outputs"], [])
                                code = compile("".join(cell["source"]), f"{name}:cell-{index}", "exec")
                                exec(code, namespace)
                        if name.startswith("01"):
                            self.assertNotIn("api_key", namespace)
                    self.assertEqual(namespace["summary"]["devices"], 2)
                    self.assertEqual(namespace["summary"]["jv_measurements"], 1)
                    self.assertEqual(len(namespace["preview"]), 2)
                os.chdir(previous_directory)
        finally:
            os.chdir(previous_directory)


if __name__ == "__main__":
    unittest.main()
