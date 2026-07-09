# Repository Governance

このリポジトリは、1人または少人数で運用しやすい軽量な OSS 運用を基本にします。
ただし、`main` は配布済みの安定版として扱い、直接 push しません。

## Branch Policy

| Branch | 役割 | ルール |
| --- | --- | --- |
| `main` | リリース済みの安定版 | 直接 push 禁止。原則 `release/v*`、緊急時のみ `hotfix/*`、Dependabot security PR のみを PR で受け入れる |
| `develop` | 次リリース向けの統合ブランチ | 通常の機能追加・修正・依存関係更新は PR でここへ入れる |
| `feature/*` | 機能追加 | `develop` から作成し、PR も `develop` 向け |
| `fix/*` | 不具合修正 | `develop` から作成し、PR も `develop` 向け |
| `chore/*` / `docs/*` | 運用・文書・雑務 | 原則 `develop` 向け。リリース運用そのものを直す場合だけ `main` への bootstrap PR を許容 |
| `release/vX.Y.Z` | リリース準備 | `origin/develop` から作成し、バージョン・CHANGELOG・リリース直前の修正だけを入れて `main` へ PR |
| `hotfix/*` | 配布済み版への緊急修正 | `origin/main` から作成し、`main` へ PR。merge 後に `develop` へ取り込む |
| `dependabot/*` | 依存関係の security update | GitHub の仕様上 security PR は default branch に来るため、`main` への PR を許容。merge 後に必要なら `develop` へ取り込む |

ブランチ名に作業ツール名や個人環境名を入れません。目的が伝わる `feature/...`、`fix/...`、`release/v...` を使います。

## Required Checks

PR では最低限、以下の GitHub Actions を通します。

- `Backend Syntax`
- `Frontend Build`
- `Tauri Smoke Build`
- `Main PR Source` (`main` 向け PR のみ)

ローカルでは変更内容に応じて以下を使います。

```bash
./scripts/oss_preflight.sh
SKIP_GUI_BUILD=1 ./scripts/oss_preflight.sh
uv run python -m pytest -q
```

## Pull Request Rules

- `main` / `develop` への変更は PR 経由にします。
- `main` 向け PR は `release/v*`、`hotfix/*`、`dependabot/*` 以外を禁止します。
- PR は小さく保ち、目的・変更内容・確認結果・既知の制約を書きます。
- review thread は merge 前に解消します。
- merge 方法は `merge commit` または `squash merge` に限定します。

## Release Flow

1. `develop` に次リリースへ入れる変更を集約します。
2. `origin/develop` から `release/vX.Y.Z` を作成します。

   ```bash
   git fetch origin
   git switch -c release/vX.Y.Z origin/develop
   ```

3. release branch 上で release helper を実行します。

   ```bash
   bash scripts/release.sh X.Y.Z
   ```

   この script は version bump、CHANGELOG 更新、ローカル build、release commit、release branch の push までを行います。
   `main` への push、tag 作成、GitHub Release 作成は行いません。

4. `release/vX.Y.Z` から `main` へ PR を作成します。
5. required checks が成功したら merge します。
6. merge 後の `main` commit に release tag を作成して push します。

   ```bash
   git switch main
   git pull --ff-only origin main
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin vX.Y.Z
   ```

7. tag push により `Release DMG` workflow が DMG / updater artifacts / GitHub Release asset を作成します。
8. `main` の release commit を `develop` に取り込みます。

   ```bash
   git switch -c sync/vX.Y.Z-back-to-develop origin/develop
   git merge --no-ff origin/main
   git push -u origin sync/vX.Y.Z-back-to-develop
   ```

   その後、`sync/vX.Y.Z-back-to-develop` から `develop` へ PR を作成します。

## Hotfix Flow

1. `origin/main` から `hotfix/<short-topic>` を作成します。
2. 修正と必要な検証を行い、`hotfix/*` から `main` へ PR を作成します。
3. merge 後に tag / release を作成します。
4. 同じ修正を `develop` に取り込みます。conflict する場合は PR で明示します。

## Current GitHub Rules

2026-07-09 時点の運用前提です。

- `protect main`: active
  - deletion 禁止
  - non-fast-forward 禁止
  - PR 必須
  - review thread 解消必須
  - required status checks: `Backend Syntax`, `Frontend Build`, `Tauri Smoke Build`
- `protect develop`: active
  - deletion 禁止
  - non-fast-forward 禁止
  - PR 必須
  - review thread 解消必須
  - required status checks: `Backend Syntax`, `Frontend Build`, `Tauri Smoke Build`
- `protect release tags`: active
  - `refs/tags/v*` の deletion / update 禁止

`Main PR Source` workflow を `main` に入れた後、`protect main` の required status checks に `Main PR Source` も追加します。
