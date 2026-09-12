# Security

- **Report a vulnerability:** open a GitHub issue labeled `security` on
  `anki-boi/onlinejobs.ph-suite`. There is no public disclosure process or
  bug-bounty program — this is a single-maintainer personal tool.
- **Surface area:** the server binds `127.0.0.1` (loopback only) by default.
  Run it on another interface only if you know why.
- **Credentials:** API keys and cookies live in `config.local.json`, which is
  git-ignored. The checked-in `config.json` must never contain secrets.
  `GET /api/config` redacts `llm_api_key` and cookie values.
- **Data:** all job data lives in `jobs.db` (SQLite, git-ignored). Nightly
  copies go to `backups/` (git-ignored). Nothing is uploaded anywhere.
- **Dependencies:** see `requirements.txt`. Pin new dependencies; the install
  flow (`install.bat`) installs exactly those versions.
