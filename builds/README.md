# builds/

Compiled `.cs3` plugin files and `plugins.json` (the raw plugin *list*)
land here automatically whenever the **Generate Provider** or
**Build All Plugins** workflows run.

Install in CloudStream: Settings → Extensions → Add repository, then use
the **repository manifest** in the repo root — not this folder's
`plugins.json`:

```
https://raw.githubusercontent.com/devfahim00/AutoCloudStream/main/repo.json
```

or download a single `.cs3` from this folder and sideload it manually.
