#!/usr/bin/env bash
# Manual release helper.
#
#   bash scripts/release.sh 0.2.0-beta.1
#
# Steps it performs (in order):
#   1. validate args / git state / tag uniqueness
#   2. bump version in gui/package.json, gui/src-tauri/tauri.conf.json,
#      gui/src-tauri/Cargo.toml (and let cargo update Cargo.lock at build)
#   3. pnpm build  (= bundle backend + tauri build + sign + notarize + staple .app/DMG)
#   4. prepend CHANGELOG.md entry with auto-generated git log notes
#      (edits opened in $EDITOR; user can refine before commit)
#   5. commit "chore(release): vX.Y.Z" + annotated tag vX.Y.Z
#   6. push commit + tag to origin/main
#   7. gh release create vX.Y.Z --notes-file <changelog-section> with the DMG
#
# Re-run safety: validates state before any destructive write; aborts on errors.
#
set -euo pipefail

NEW_VERSION="${1:-}"
if [ -z "$NEW_VERSION" ]; then
  echo "Usage: $0 <version>  (e.g. 0.2.0-beta.1)"
  exit 1
fi
TAG="v${NEW_VERSION}"

# semver-ish validation (M.m.p or M.m.p-prerelease)
if ! printf '%s' "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.-]*)?$'; then
  echo "Error: '$NEW_VERSION' does not look like semver"
  exit 1
fi

# Tauri が DMG ファイル名に使うのは major.minor.patch のみ (pre-release suffix は落ちる)
SHORT_VERSION="${NEW_VERSION%%-*}"
DMG_PATH="gui/src-tauri/target/release/bundle/dmg/Seam_${SHORT_VERSION}_aarch64.dmg"

# ─── 事前チェック ─────────────────────────────────────
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "main" ]; then
  echo "Error: must be on 'main' (currently on '$BRANCH')"
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Error: working tree is not clean. commit or stash first."
  exit 1
fi
if git rev-parse "refs/tags/$TAG" >/dev/null 2>&1; then
  echo "Error: tag '$TAG' already exists locally"
  exit 1
fi
if git ls-remote --tags --exit-code origin "refs/tags/$TAG" >/dev/null 2>&1; then
  echo "Error: tag '$TAG' already exists on origin"
  exit 1
fi

# 直近の release タグ (notes 生成範囲)
PREV_TAG=$(git tag --list 'v*' --sort=-v:refname | head -1 || true)

echo "[release] new version : $NEW_VERSION  (tag: $TAG)"
echo "[release] previous tag: ${PREV_TAG:-<none>}"
echo "[release] DMG output  : $DMG_PATH"
echo ""

# ─── バージョン書き換え ──────────────────────────────
echo "[release] bumping versions..."

# gui/package.json (最初の "version": "..." を置換)
node -e '
  const fs = require("fs");
  const p = "gui/package.json";
  const j = JSON.parse(fs.readFileSync(p, "utf8"));
  j.version = process.argv[1];
  fs.writeFileSync(p, JSON.stringify(j, null, 2) + "\n");
' "$NEW_VERSION"

# gui/src-tauri/tauri.conf.json (同上)
node -e '
  const fs = require("fs");
  const p = "gui/src-tauri/tauri.conf.json";
  const j = JSON.parse(fs.readFileSync(p, "utf8"));
  j.version = process.argv[1];
  fs.writeFileSync(p, JSON.stringify(j, null, 2) + "\n");
' "$NEW_VERSION"

# gui/src-tauri/Cargo.toml の [package] version のみ置換
awk -v ver="$NEW_VERSION" '
  BEGIN { in_pkg=0; done=0 }
  /^\[package\]/ { in_pkg=1; print; next }
  /^\[/ && !/^\[package\]/ { in_pkg=0 }
  {
    if (in_pkg && !done && $0 ~ /^version[[:space:]]*=/) {
      print "version = \"" ver "\""
      done=1
      next
    }
    print
  }
' gui/src-tauri/Cargo.toml > gui/src-tauri/Cargo.toml.new
mv gui/src-tauri/Cargo.toml.new gui/src-tauri/Cargo.toml

echo "[release] versions updated"

# ─── DMG ビルド (.app/DMG の署名・公証・staple まで一気通貫) ──────
echo "[release] building DMG (this can take 5-15 min for notarization)..."
pnpm build

if [ ! -f "$DMG_PATH" ]; then
  echo "Error: expected DMG not found at $DMG_PATH"
  echo "       check pnpm build output for actual filename"
  exit 1
fi

# ─── CHANGELOG エントリ生成 ─────────────────────────
DATE=$(date +%Y-%m-%d)
RANGE_DESC=""
if [ -n "$PREV_TAG" ]; then
  RANGE="${PREV_TAG}..HEAD"
  RANGE_DESC="(since $PREV_TAG)"
else
  # 初回 release: HEAD までの全コミットを使う
  RANGE=""
fi

GIT_LOG_NOTES=$(
  if [ -n "$RANGE" ]; then
    git log --pretty=format:"- %s" "$RANGE"
  else
    git log --pretty=format:"- %s" -50
  fi
)

# CHANGELOG.md を冪等に作成・前置
if [ ! -f CHANGELOG.md ]; then
  cat > CHANGELOG.md <<EOF
# Changelog

All notable changes to Seam are documented here.
Format roughly follows [Keep a Changelog](https://keepachangelog.com/).

EOF
fi

NEW_ENTRY=$(cat <<EOF
## [${NEW_VERSION}] - ${DATE} ${RANGE_DESC}

${GIT_LOG_NOTES}

EOF
)

# header (最初の "# Changelog" ブロック) を残しつつ、その直後に新エントリを挿入
TMP=$(mktemp)
awk -v entry="$NEW_ENTRY" '
  BEGIN { inserted=0 }
  /^## \[/ && !inserted {
    print entry
    inserted=1
  }
  { print }
  END {
    if (!inserted) print entry
  }
' CHANGELOG.md > "$TMP"
mv "$TMP" CHANGELOG.md

echo "[release] CHANGELOG.md updated. opening in $EDITOR to review/refine..."
"${EDITOR:-vi}" CHANGELOG.md

# 今回のエントリだけを取り出して release notes ファイル化
NOTES_FILE=$(mktemp)
awk -v ver="$NEW_VERSION" '
  $0 ~ "^## \\[" ver "\\]" { capture=1; next }
  capture && /^## \[/ { exit }
  capture { print }
' CHANGELOG.md > "$NOTES_FILE"

# ─── commit & tag ────────────────────────────────────
echo "[release] committing version bump + changelog..."
git add gui/package.json gui/src-tauri/tauri.conf.json gui/src-tauri/Cargo.toml \
        gui/src-tauri/Cargo.lock CHANGELOG.md 2>/dev/null || true
git commit -m "chore(release): ${TAG}"
git tag -a "$TAG" -m "Release ${TAG}"

# ─── push ────────────────────────────────────────────
echo "[release] pushing main + tag..."
git push origin main
git push origin "$TAG"

# ─── GitHub Release ──────────────────────────────────
echo "[release] creating GitHub release..."
# pre-release 判定: バージョン文字列に '-' が含まれていれば prerelease
PRERELEASE_FLAG=""
case "$NEW_VERSION" in
  *-*) PRERELEASE_FLAG="--prerelease" ;;
esac

gh release create "$TAG" \
  --title "$TAG" \
  --notes-file "$NOTES_FILE" \
  $PRERELEASE_FLAG \
  "$DMG_PATH"

rm -f "$NOTES_FILE"

echo ""
echo "[release] done."
echo "          tag    : $TAG"
echo "          DMG    : $DMG_PATH"
gh release view "$TAG" --json url --jq '"          URL    : " + .url'
