# class-watcher

Checks UCSB's public class-data service for an open seat in one class and sends a
phone alert (ntfy) when one appears. Read-only: it never logs in to GOLD and never
enrols in anything.

- `watch.py` – the checker. Settings are at the top of the file.
- `.github/workflows/watch.yml` – runs a check every 5 minutes on GitHub.
- Needs two repository secrets: `UCSB_API_KEY` and `NTFY_TOPIC`.
