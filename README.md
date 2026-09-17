# Solar.ChemDX data API

Download device records from [Solar.ChemDX](https://solar.chemdx.org), filter by
measurement type, and export recipes and JV measurements to CSV.

Also available: [recipe input template](template_input.xlsx) and
[external reference datasets](ExternalData/README.md).

## Quick start

Use **Python 3.8 or later**. Download and extract **Code → Download ZIP**, or clone:

```sh
git clone https://github.com/KRICT-DATA/solarcell.git
cd solarcell
```

Get your API key from **Account → API KEY** on Solar.ChemDX. Use **Generate** if
you do not have one; an existing key can be reused. If it shows **Not allowed**,
contact the platform administrator.

```sh
python -m pip install -r requirements.txt
python solarcell.py download
python solarcell.py csv
```

Enter your account email and API key when prompted. The key is hidden.
The commands download all groups available to your account and save
**devices.csv** and **jv.csv** in the printed output folder.

To select groups or convert an existing JSON file:

```sh
python solarcell.py download --groups 2 3
python solarcell.py csv --input my_records.json --output exports/my-result
```

For automated runs, set `SOLARCELL_EMAIL` and `SOLARCELL_API_KEY`.

## Measurement filters

```sh
python solarcell.py csv --measurement SEM
python solarcell.py csv --measurement JV
```

Omit `--measurement` to keep all devices. Filtering runs locally after download.
Keys are case-insensitive and matched exactly: `PL` does not include `TRPL`.
Check the `measurement_types` column for available types.

## CSV output

| File | Contents |
| --- | --- |
| `devices.csv` | One row per device: recipe fields, measurement types, and the best-efficiency JV summary |
| `jv.csv` | One row per JV entry: metrics, original values, and measurement arrays |

`Ef_max`, `FF_max`, `JS_max`, and `V0_max` come from the same best-efficiency JV
entry. Devices without JV data remain in `devices.csv`; missing values stay empty.

Recipe columns use JSON paths such as `input.0.annealing.1.temperature.value`,
replacing the old `BL_*`, `ETL_*`, `PV_*`, and `HTL_*` columns.
Arrays in `jv.csv` are JSON strings. Both files use UTF-8 with a BOM for Excel.

## Notebook examples

For a step-by-step example, open the notebooks in JupyterLab:

```sh
python -m pip install -r requirements-notebooks.txt
python -m jupyterlab
```

Run [01_download_data.ipynb](01_download_data.ipynb), then
[02_data_csv.ipynb](02_data_csv.ipynb).

Questions: [yealee@krict.re.kr](mailto:yealee@krict.re.kr).
