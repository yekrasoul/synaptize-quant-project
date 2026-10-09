# Development and change-control workflow

All code/config/schema changes use:

```text
branch → focused tests → pull request → Canonical validation → review → merge
```

The `Canonical validation` required check runs compileall, the full unit suite, and `btc-dca validate`. Do not commit or push development changes directly to `main`. Keep changes focused and preserve existing user work. A merge is not authorization to activate live execution or change strategy rules.

Repository code cannot enforce GitHub branch protection or rulesets. Maintainers should configure the GitHub repository externally to require pull requests, required checks, and appropriate review before merging. Do not attempt to alter GitHub security settings from application code or embed credentials to do so.
