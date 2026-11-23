# Manhua Raws Downloader

A desktop app for searching and downloading Chinese manhua raws.

## Install and run

Python 3.12 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

On Windows, activate with `.venv\Scripts\activate` instead of `source .venv/bin/activate`.

## Use

1. Search by title, or paste a link from a supported site.
2. Open a result to see its details and chapter list.
3. Select chapters, choose Folder or CBZ, then click Download.
4. Progress, skipped chapters, and any failures show in the download panel.

Downloads go to `downloads/<source>/<series>/<chapter>/`. Covers are cached in `.cache/covers`.

## Sources

Sources are enabled in `config.yaml` under `sources.<id>.enabled`. Some sites need an account: set their credentials in the same file (for example `sources.one99mh.email` and `password`, or a logged-in `cookie`). If a site is blocked in your region, set `network.proxy`.

## Downloads

- Pages are numbered from `001`, so a missing page leaves a visible gap.
- Failed pages are retried, then resolved again, then requested from another source that has the same series. A chapter with zero pages is never saved.
- Complete chapters are skipped on re-run. Delete `downloads/.manhua-dl/` to force a full re-download.

All settings (output folder, formats, concurrency, retries, proxy, cache, sources) live in `config.yaml`.
