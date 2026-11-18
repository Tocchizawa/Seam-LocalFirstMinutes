#!/usr/bin/env bash
set -e

APP_PATH="gui/src-tauri/target/release/bundle/macos/Seam.app"
DMG_DIR="gui/src-tauri/target/release/bundle/dmg"
ENTITLEMENTS="gui/src-tauri/entitlements.plist"

# Tauri が DMG ファイル名に使うバージョンは tauri.conf.json の major.minor.patch のみ
# (pre-release suffix "-beta.x" は落とされる)。配布物の命名と一致させるため
# ここでも同じ規則で導出する。
TAURI_VERSION=$(node -e 'console.log(require("./gui/src-tauri/tauri.conf.json").version)')
SHORT_VERSION="${TAURI_VERSION%%-*}"
DMG_PATH="${DMG_DIR}/Seam_${SHORT_VERSION}_aarch64.dmg"
SIGN_ID="${APPLE_SIGNING_IDENTITY:-}"
REQUIRE_NOTARIZATION="${RELEASE_DMG_REQUIRE_NOTARIZATION:-0}"

cleanup() {
  rm -rf "${STAGE_DIR:-}" "${NOTARY_TMP_DIR:-}"
}
trap cleanup EXIT

has_notary_credentials() {
  [ -n "${APPLE_ID:-}" ] \
    && [ -n "${APPLE_APP_PASSWORD:-}" ] \
    && [ -n "${APPLE_TEAM_ID:-}" ]
}

require_release_notarization() {
  if [ "$REQUIRE_NOTARIZATION" != "1" ]; then
    return 0
  fi
  if [ -z "$SIGN_ID" ]; then
    echo "Error: RELEASE_DMG_REQUIRE_NOTARIZATION=1 requires APPLE_SIGNING_IDENTITY"
    exit 1
  fi
  if ! has_notary_credentials; then
    echo "Error: RELEASE_DMG_REQUIRE_NOTARIZATION=1 requires APPLE_ID / APPLE_APP_PASSWORD / APPLE_TEAM_ID"
    exit 1
  fi
}

notarize_artifact() {
  local artifact_path="$1"
  local label="$2"

  echo "[notarize] submitting ${label} to Apple (これに数分〜十数分かかります)..."
  set +e
  xcrun notarytool submit "$artifact_path" \
    --apple-id "$APPLE_ID" \
    --password "$APPLE_APP_PASSWORD" \
    --team-id "$APPLE_TEAM_ID" \
    --wait
  local notary_exit=$?
  set -e
  if [ "$notary_exit" -ne 0 ]; then
    echo "Error: notarization failed for ${label} (exit $notary_exit)"
    exit "$notary_exit"
  fi
}

if [ ! -d "$APP_PATH" ]; then
  echo "Error: $APP_PATH not found"
  exit 1
fi

require_release_notarization

# ─── 既存 DMG が最新かつ公証・staple 済みなら全スキップ ─
# Apple ID をリトライで不要に notarytool に投げないための安全弁。
# .app の主バイナリより DMG が新しく、staple ticket が有効なら何もしない。
# 強制再ビルドしたければ DMG を削除して再実行する。
if [ -f "$DMG_PATH" ] \
  && [ "$DMG_PATH" -nt "$APP_PATH/Contents/MacOS/gui" ] \
  && xcrun stapler validate "$APP_PATH" >/dev/null 2>&1 \
  && xcrun stapler validate "$DMG_PATH" >/dev/null 2>&1; then
  echo "[skip] $DMG_PATH は最新かつ公証・staple 済みです"
  echo "       強制再生成するには DMG を削除してから再実行してください"
  ls -lh "$DMG_PATH"
  exit 0
fi

# ─── macOS 権限キーを Info.plist に注入 ─────────────────
# Tauri は Info.plist のカスタムキーをサポートしないため、ここで PlistBuddy を使って追加する。
# これがないと macOS はマイク/システム音声取得を silent deny するため、録音が無音になる。
# 重要: 署名前に必ず実行すること (署名後に Info.plist を変えると署名が無効化する)。
PLIST="${APP_PATH}/Contents/Info.plist"

add_plist_string() {
  local key="$1"; shift
  local value="$*"
  /usr/libexec/PlistBuddy -c "Set :${key} ${value}" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :${key} string ${value}" "$PLIST"
}

add_plist_string "NSMicrophoneUsageDescription" "会議音声を録音して文字起こしするためマイクを使用します"
add_plist_string "NSAudioCaptureUsageDescription" "会議参加者の音声を取り込むためシステム音声を取得します"
add_plist_string "NSAppleEventsUsageDescription" "音声デバイスの操作のため使用します"

echo "[plist] injected privacy keys"

# ─── 署名 ───────────────────────────────────────────────
# APPLE_SIGNING_IDENTITY が定義されていれば Developer ID で署名 (= 配布可)。
# 未定義なら従来通り adhoc 署名 (= ローカル動作のみ可、配布不可)。

if [ -z "$SIGN_ID" ]; then
  echo "[codesign] APPLE_SIGNING_IDENTITY 未設定 → adhoc 署名 (公証スキップ)"
  codesign --force --deep --sign - "$APP_PATH" 2>/dev/null || true
  echo "[codesign] adhoc 署名完了"
else
  if [ ! -f "$ENTITLEMENTS" ]; then
    echo "Error: entitlements not found: $ENTITLEMENTS"
    exit 1
  fi

  # 1. ネストバイナリ (uv 等) を先に個別署名
  #    Apple の最新公証は --deep を信頼せず、各 nested binary に
  #    hardened runtime + secure timestamp を要求する。
  NESTED_BINARIES=(
    "$APP_PATH/Contents/Resources/uv"
    "$APP_PATH/Contents/Resources/audio-capture"
  )
  for bin in "${NESTED_BINARIES[@]}"; do
    if [ -f "$bin" ]; then
      # nested binary にも entitlements を渡し、子プロセス (python 等) も
      # 同じ exception (library validation 無効化等) を継承できるようにする。
      codesign --force --options runtime --timestamp \
        --entitlements "$ENTITLEMENTS" \
        --sign "$SIGN_ID" \
        "$bin"
      echo "[codesign] signed nested: ${bin#$APP_PATH/}"
    fi
  done

  # 2. .app 本体を署名 (entitlements + hardened runtime + timestamp)
  codesign --force --options runtime --timestamp \
    --entitlements "$ENTITLEMENTS" \
    --sign "$SIGN_ID" \
    "$APP_PATH"
  echo "[codesign] signed: $SIGN_ID"

  # 検証
  codesign --verify --deep --strict --verbose=2 "$APP_PATH" \
    && echo "[codesign] verify OK"

  if ! has_notary_credentials; then
    echo "Error: Developer ID 署名には .app / DMG の公証が必要です"
    echo "       APPLE_ID / APPLE_APP_PASSWORD / APPLE_TEAM_ID を設定してください"
    echo "       ローカル検証だけなら APPLE_SIGNING_IDENTITY を unset してください"
    exit 1
  fi

  # .app 単体でも Gatekeeper が検証できるよう、DMG 作成前に app bundle を
  # zip で公証提出し、ticket を app に staple しておく。
  NOTARY_TMP_DIR=$(mktemp -d)
  APP_ZIP_PATH="$NOTARY_TMP_DIR/Seam.app.zip"
  ditto -c -k --keepParent "$APP_PATH" "$APP_ZIP_PATH"
  notarize_artifact "$APP_ZIP_PATH" "Seam.app"

  echo "[notarize] stapling app..."
  xcrun stapler staple "$APP_PATH"
  xcrun stapler validate "$APP_PATH" && echo "[notarize] app stapled OK"
fi

# ─── DMG 作成 ───────────────────────────────────────────
# .app 単体にも ticket を持たせたうえで DMG に詰める。コピー時に ticket や
# 拡張属性を落とさないよう、app bundle には ditto を使う。
mkdir -p "$DMG_DIR"
rm -f "$DMG_PATH"

STAGE_DIR=$(mktemp -d)

ditto "$APP_PATH" "$STAGE_DIR/Seam.app"
ln -s /Applications "$STAGE_DIR/Applications"

# "Seam" や "Seam Installer" は macOS が /Volumes/<name>/Seam.app へのアクセスを
# "Operation not permitted" で拒否することがあるため、衝突しない名前を使う。
hdiutil create -volname "Seam Setup" -srcfolder "$STAGE_DIR" -ov -format UDZO "$DMG_PATH"
echo "[dmg] created: $DMG_PATH"

# ─── DMG 自体を Developer ID で署名 ─────────────────────
# Gatekeeper が DMG のダウンロード時に署名チェックする経路に備える。
# adhoc 署名の場合はスキップ。
if [ -n "$SIGN_ID" ]; then
  codesign --force --sign "$SIGN_ID" --timestamp "$DMG_PATH"
  echo "[codesign] DMG signed"
fi

# ─── 公証 (Notarization) — DMG に対して行う ─────────────
if [ -z "$SIGN_ID" ]; then
  echo "[notarize] adhoc 署名のため公証スキップ"
else
  notarize_artifact "$DMG_PATH" "DMG"
  echo "[notarize] stapling DMG..."
  xcrun stapler staple "$DMG_PATH"
  xcrun stapler validate "$DMG_PATH" && echo "[notarize] DMG stapled OK"
fi

echo ""
echo "DMG created: $DMG_PATH"
ls -lh "$DMG_PATH"
