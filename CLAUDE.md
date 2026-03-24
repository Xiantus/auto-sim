# Git Branching Workflow

## Branch Strategy

- **`master`** — production/release branch. Only receives merges from `develop`.
- **`develop`** — integration branch for local testing. All feature and fix branches merge here first.
- **Feature/fix branches** — branch off `develop`, merge back into `develop` when done.

## Rules

1. Never commit directly to `master`.
2. Create feature/fix branches from `develop`:
   ```
   git checkout develop
   git checkout -b feature/my-feature
   ```
3. Merge completed work into `develop` first:
   ```
   git checkout develop
   git merge feature/my-feature
   ```
4. To release, merge `develop` into `master`:
   ```
   git checkout master
   git merge develop
   git push
   ```
