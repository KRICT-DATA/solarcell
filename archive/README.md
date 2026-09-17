# Archive

Superseded versions, kept so that a change can be compared against what it
replaced without digging through history. Nothing here is maintained, and
nothing in the repository reads from it.

| Folder | What it holds |
| --- | --- |
| `2026-09-17-before-python-cli/` | `README.md` and both notebooks as they stood before pull request #1 added `solarcell.py` / `solarcell_tools.py`. The notebooks still carry their stored cell outputs. |

Each folder has a matching annotated tag with the same name, so the complete
tree — not only the files copied here — is recoverable:

```sh
git show archive/2026-09-17-before-python-cli
git checkout archive/2026-09-17-before-python-cli -- <path>
```

Use the tag when you need the whole state; use these copies when you only want
to read or diff a file.
