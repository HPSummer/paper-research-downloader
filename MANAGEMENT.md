# Release Management

## Scope

The repository contains a clean distributable skill folder plus release artifacts.

- `paper-research-downloader/`: installable Codex/Claude skill.
- `dist/`: packaged release zip and release metadata.
- `README.md`: repository-facing installation and usage notes.

Do not commit local `config.local.json`, caches, downloaded papers, generated notes, or temporary run outputs.

## Release Checklist

1. Bump `VERSION` and `USER_AGENT` in `scripts/paper_research_downloader.py`.
2. Update `SKILL.md` and relevant files under `references/`.
3. Run:

```powershell
python .\paper-research-downloader\scripts\paper_research_downloader.py test
python .\paper-research-downloader\scripts\paper_research_downloader.py self-test
python C:\Users\Admin\.codex\skills\.system\skill-creator\scripts\quick_validate.py .\paper-research-downloader
python .\paper-research-downloader\scripts\paper_research_downloader.py package --out-dir .\dist
```

4. Verify the zip excludes `config.local.json`.
5. Tag the release as `vX.Y.Z`.

