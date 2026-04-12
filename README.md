# Coral — Table Soccer Tournament Tracker

Scrapes tournament data from [app.tablesoccer.org](https://app.tablesoccer.org) and generates HTML reports for Danish players.

## Setup

```bash
uv sync
uv run playwright install chromium
cp .env.example .env  # fill in CORAL_USERNAME and CORAL_PASSWORD
```

## Usage

```bash
# Show your profile
uv run main.py profile

# Show tournament summary
uv run main.py tournament <CODE>

# Generate HTML report (writes to docs/index.html for GitHub Pages)
uv run report.py <CODE>

# Watch live tournament
uv run main.py watch <CODE>
```

## GitHub Pages

Reports are published automatically via GitHub Pages from the `docs/` directory on `master`. To update:

```bash
uv run report.py <CODE>
git add docs/index.html
git commit -m "Update report"
git push
```
