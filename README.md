# Solar.ChemDX data API

Download perovskite solar cell device records from [Solar.ChemDX](https://solar.chemdx.org),
select devices by measurement type, and convert their recipes and JV measurements to CSV.
This repository also provides the [recipe input template](template_input.xlsx) and
[external reference datasets](ExternalData/README.md).

| File | Purpose |
| --- | --- |
| [solarcell.py](solarcell.py) | Run downloads and CSV conversion directly with Python |
| [solarcell_tools.py](solarcell_tools.py) | Shared, tested download and conversion functions |
| [requirements.txt](requirements.txt) | Dependencies for the Python commands (`requests`) |
| [template_input.xlsx](template_input.xlsx) | Template for entering device fabrication recipes |
| [ExternalData](ExternalData/README.md) | Contributed and calculated reference data |

## Quick start

Use **Python 3.10 or later**. Download the entire repository with **Code → Download ZIP**
and extract it, or clone it:

```sh
git clone https://github.com/KRICT-DATA/solarcell.git
cd solarcell
```

Sign in to Solar.ChemDX and open **Account → API KEY → Generate**. If your account
shows **Not allowed**, contact the platform administrator. From the repository folder,
install the dependency and run these Python commands:

```sh
python -m pip install -r requirements.txt
python solarcell.py download
python solarcell.py csv
```

The download command prompts for your account email and API key; the key is hidden.
It downloads every group available to your account. The CSV command converts the
latest complete download and prints the folder containing **devices.csv** and **jv.csv**.
These commands require Python and `requests`; JupyterLab and pandas are optional.

### Select groups or measurements

```sh
# Download only selected groups (IDs must be available to your account).
python solarcell.py download --groups 2 3

# Convert devices with SEM measurements from the latest download.
python solarcell.py csv --measurement SEM

# Convert a specific existing JSON file to a new output directory.
python solarcell.py csv --input my_records.json --output exports/my-result

# Save both the full download and a separate filtered_records.json.
python solarcell.py download --measurement SEM

# Show all command options.
python solarcell.py download --help
python solarcell.py csv --help
```

`--measurement` matches an exact key, ignoring case. Omit it to keep all devices.
The latest-download pointer always refers to the **full** snapshot; to convert the
filtered JSON, pass its printed path with `--input`, or use `csv --measurement SEM`.
When downloading to a custom folder with `download --output-root my_downloads`,
use `csv --download-root my_downloads` to find that folder's latest snapshot.

The commands load the selected dataset into memory. For a large collection, start
with a few `--groups`; filtering by measurement happens after the JSON has loaded
and does not reduce download size or initial memory use.

For unattended use, set `SOLARCELL_EMAIL` and `SOLARCELL_API_KEY` in the environment
before running Python. An email can also be provided with `--email`; the API key is
read from the environment or the hidden prompt. Generated downloads, exports, `.env`
files and notebook checkpoints are excluded from Git. A failed command exits with
a nonzero status and prints its error to the terminal.

**한국어 안내:** 저장소 전체를 내려받아 `python -m pip install -r requirements.txt`로
설치한 뒤, `python solarcell.py download` → `python solarcell.py csv` 순서로 실행하세요.
사이트 Account에서 발급한 API 키를 입력하면 계정에 제공되는 데이터를 내려받습니다.
`python solarcell.py csv --measurement SEM`으로 SEM 측정이 있는 장치만 변환할 수 있으며,
결과는 `exports` 폴더에 저장됩니다.

## API request and response

```http
POST https://solar.chemdx.org/api/v1/search
Content-Type: application/json

{"email": "<account email>", "api_key": "<API key>"}
```

A successful response is an array of available groups, for example:

```json
[
  {
    "id": 2,
    "name": "Example group",
    "temporaryUrl": "https://<storage-host>/<archive>.zip?<temporary-signature>"
  }
]
```

- Each URL downloads a ZIP of **device JSON records**. Access depends on the
  account's group-read permissions: a full group archive (`<id>.zip`) or a public
  subset (`<id>-public.zip`). Groups with no available archive may be absent.
- Signed URLs normally expire after about **five minutes**. The downloader refreshes
  a group's URL once if its download returns HTTP 403.
- Wrong credentials can return **HTTP 200** with
  `{"status":"failed","message":"Invalid user"}`. The downloader checks both
  HTTP status and response content.
- `--groups` selects which returned archives to download. Measurement filtering is
  performed **locally after download**; these command options are not API query parameters.
- JSON may contain measurement values or attachment metadata. The examples do not
  separately download original SEM images or other measurement attachments.

## Downloaded records and measurement filters

Each run creates an independent directory:

```text
downloads/
  latest.json                         # Pointer to the latest successful full snapshot
  run-<UTC timestamp>-<unique suffix>/
    2.zip                             # Original group archive, retained
    2/                                # Extracted JSON, including any archive subfolders
    records.json                      # Full device objects in one JSON array
    filtered_records.json             # Written only when a measurement filter is used
```

The downloader reads JSON recursively **inside the archives extracted for this run**.
It never scans the working directory for ZIPs or deletes unrelated archives. It stops
on malformed records or conflicting duplicates. A failed run leaves the previous
`latest.json` pointer intact; its partial run directory can be inspected locally.
Repeated successful runs keep separate snapshots, so stale records are not merged in.

Common record fields:

| Field | Meaning |
| --- | --- |
| `id` | Device identifier that links a recipe with its measurements |
| `group` | Group identifier, when present |
| `author` | Author value supplied by the API; no hard-coded name lookup is applied |
| `input` | Fabrication recipe, usually an ordered list of steps |
| `analysis` | Parsed measurements, keyed by measurement type |
| `analysisInfo` | Measurement/attachment metadata, when available |

The `measurement_types` column lists the measurement types found for each device.
Examples include `JV`, `UVVIS`, `XRD`, `PL`, `STABILITY`, `SEM`, `TRPL`, `GIWAXS` and `ADHESION`.
Filters inspect non-empty entries in both `analysis` and `analysisInfo` and match
keys exactly, ignoring case. For example, `PL` does not also match `TRPL`.
An attachment metadata match does not guarantee parsed numeric measurements exist.

## CSV output

Both CSVs use UTF-8 with a BOM for Excel and contain no dataframe index column.
Each execution writes a new export directory. An empty selection creates header-only
CSVs. Every selected device is retained, including devices without JV measurements.

| Output | Rows and fields |
| --- | --- |
| `devices.csv` | One row per device; identifiers, measurement types, common recipe fields, all nested recipe fields and the best-efficiency JV summary |
| `jv.csv` | One row per JV entry; identifiers, zero-based measurement index, numeric metrics, original measurement values and arrays |

Recipe columns retain their JSON path, such as
`input.0.annealing.1.temperature.value` and `input.0.annealing.1.temperature.unit`.
Numeric path components are zero-based list positions, so recipe order is preserved.
Literal `~` and `.` in source keys are escaped as `~0` and `~1` to avoid collisions.
In `jv.csv`, lists such as voltage/current arrays are JSON strings in individual
cells; use `json.loads(cell)` to read them back.

The summary columns retain familiar names:

- `Ef_max`: largest finite JV efficiency for the device.
- `FF_max`, `JS_max`, `V0_max`: fill factor, Jsc and Voc from **that same measurement**.
- `best_jv_index`: its zero-based index in the original JV entries; the first wins a tie.
- `JV_count`: total number of JV entries, including entries without valid summary metrics.

Both older flat JV entries and entries nested under `pv_cell_parameters` are supported.
The former merged format with a top-level `JV` field is also accepted. Missing,
non-numeric or non-finite numeric summaries stay empty; genuine zeros are retained.
Original values and units remain available in the raw columns/JSON. No unit
conversion, author-name inference or scientific quality exclusion is applied.

**Schema change:** the new recipe path columns replace the old hand-built `BL_*`,
`ETL_*`, `PV_*` and `HTL_*` layout. Update downstream scripts that depend on those
columns. The [archived CSV notebook](legacy/02_data_csv_legacy.ipynb) preserves the
previous code for reference, including its historical dependencies and assumptions;
it is not part of the supported workflow. New output files are named `devices.csv`
and `jv.csv`, replacing the old default `test.csv`.

## Optional notebooks

For step-by-step exploration, install the separate notebook dependencies:

```sh
python -m pip install -r requirements-notebooks.txt
python -m jupyterlab
```

This adds pandas and JupyterLab alongside the core dependency. Run Jupyter from
the repository folder, then execute the cells in order:

1. [01_download_data.ipynb](01_download_data.ipynb): download and inspect data.
   Set `GROUP_IDS` or `MEASUREMENT` to select groups or measurements, or keep `None` for all.
2. [02_data_csv.ipynb](02_data_csv.ipynb): convert the latest full download to CSV.
   Set `INPUT_JSON` for a specific file or `MEASUREMENT` to filter before conversion.

The notebooks and Python commands use the same functions and download folders, so
you can download with one and convert with the other. Keep credentials in environment
variables or enter them at the prompts, rather than saving them in notebook cells.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Authentication failed, including HTTP 200 failures | Check the email and API key shown in Account |
| TLS certificate verification fails on an institutional network | Set `REQUESTS_CA_BUNDLE` to the organisation's trusted CA certificate file before running Python; certificate verification stays enabled |
| HTTP 403 while downloading | The downloader refreshes once; if it still fails, retry the download and check account access |
| Timeout or HTTP 5xx | Check the network/service and retry; the previous complete snapshot remains available |
| No downloadable groups | Check account permissions and whether the platform has generated archives |
| `solarcell_tools` cannot be imported | Download the entire repository and run the command from its folder |
| No latest download found | Run `python solarcell.py download`, or use `csv --input` with an existing device JSON file/array |
| Invalid `latest.json` | Download again to recreate the pointer, or use `csv --input` with an existing device JSON file/array |
| Filter returns no devices | Check `measurement_types` in an unfiltered CSV; omit `--measurement` for all devices |
| Selected group is absent | Use a group available to your account, or omit `--groups` |
| Export directory already exists | Choose a new `--output` path, or omit the option to create a unique directory |
| Credentials required in a non-interactive session | Set `SOLARCELL_EMAIL` and `SOLARCELL_API_KEY` in the environment |

## Development checks

```sh
python -m unittest discover -s tests -v
```

The tests use synthetic records and mocked HTTP responses; they do not need a real
API key or contact the live service. The notebook cell check runs when pandas is
installed and is skipped in a core-only environment. Downloaded platform data should
remain outside source control.

Questions or assistance: [yealee@krict.re.kr](mailto:yealee@krict.re.kr).
