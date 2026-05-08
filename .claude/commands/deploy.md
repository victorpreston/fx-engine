# Deploy / Branch Workflow

The git workflow for this project:

```
feature/* → dev → staging → fx-prod (manual PR on GitHub)
```

## Create and merge a feature branch
```bash
git checkout dev
git checkout -b feat/your-feature-name
# ... make changes ...
git add <files>
git commit -m "feat: description of change"
git checkout dev
git merge feat/your-feature-name --no-ff -m "feat: merge your-feature-name into dev"
git checkout staging
git merge dev --no-ff -m "chore: promote dev to staging"
git push origin feat/your-feature-name dev staging
```

## Conventional commit prefixes
- `feat:` new feature
- `fix:` bug fix
- `test:` test additions/changes
- `docs:` documentation
- `ci:` CI/CD changes
- `refactor:` code restructuring
- `chore:` maintenance (merges, dependency updates)

## Branch protection
- `fx-prod` → merged manually on GitHub (never push directly)
- `staging` → merges from `dev` only
- All PRs to `dev`, `staging`, `fx-prod` run the full CI check suite
