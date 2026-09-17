Solar.Chemdx.org

This GitHub repository is for the solar.chemdx.org platform. It provides a template file for synthesis recipes of perovskite solar cell devices (template_input.xlsx), an API for bulk data download (01-download_data.ipynb), and a tool to convert JSON files into a readable format (02-data_csv.ipynb).

If you have any questions or need further assistance, please contact yealee@krict.re.kr.

---

## Usage details

### Bulk download (`01_download_data.ipynb`)

The platform exposes one API endpoint:

```
POST https://solar.chemdx.org/api/v1/search
body: { "email": "<account email>", "api_key": "<api key>" }
```

- Get your API key from **solar.chemdx.org → Account**.
- The response lists every group you may read, each with a temporary download
  URL (valid ~5 minutes) for that group's zip archive.
- Scope follows your permissions: your own group arrives as the full archive
  (`<id>.zip`), other groups as their public subset (`<id>-public.zip`).
- On wrong credentials the API returns HTTP 200 with
  `{"status":"failed","message":"Invalid user"}`; the notebook stops in that case.

Each downloaded JSON file is one device record. Useful keys:

- `id` — device ID (e.g. `KRICT-SC-00001`), which links the recipe to the
  device's measurement files.
- `input` — the fabrication recipe as entered.
- `analysis` / `analysisInfo` — attached measurements. Keys may include `JV`,
  `UVVIS`, `XRD`, `PL`, `STABILITY`, `SEM`, `TRPL`, `GIWAXS`, `ADHESION`.

To keep only records that have a given measurement (e.g. SEM), filter on the
keys of `analysis` — see the last cell of the notebook.

### Notes

- Requires Python 3.8+ with `requests`, `pandas`, `numpy`.
- On a corporate network with a TLS-inspecting proxy, certificate errors can
  occur; set `VERIFY_TLS = False` in the first notebook cell, or point
  `REQUESTS_CA_BUNDLE` to your organisation's CA bundle.
