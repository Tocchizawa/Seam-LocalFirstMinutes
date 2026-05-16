#!/usr/bin/env bash
set -e

APP_PATH="gui/src-tauri/target/release/bundle/macos/Seam.app"
DMG_DIR="gui/src-tauri/target/release/bundle/dmg"
DMG_PATH="${DMG_DIR}/Seam_0.1.0_aarch64.dmg"
ENTITLEMENTS="gui/src-tauri/entitlements.plist"

if [ ! -d "$APP_PATH" ]; then
  echo "Error: $APP_PATH not found"
  exit 1
fi

# ─── macOS 権限キーを Info.plist に注入 ─────────────────
# Tauri は Info.plist のカスタムキーをサポートしないため、ここで PlistBuddy を使って追加する。
# これがないと macOS はマイク/画面録音を silent deny するため、録音が無音になる。
# 重要: 署名前に必ず実行すること (署名後に Info.plist を変えると署名が無効化する)。
PLIST="${APP_PATH}/Contents/Info.plist"

add_plist_string() {
  local key="$1"; shift
  local value="$*"
  /usr/libexec/PlistBuddy -c "Set :${key} ${value}" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :${key} string ${value}" "$PLIST"
}

add_plist_string "NSMicrophoneUsageDescription" "会議音声を録音して文字起こしするためマイクを使用します"
add_plist_string "NSScreenCaptureUsageDescription" "会議参加者の音声を取り込むため画面録画権限を使用します"
add_plist_string "NSAppleEventsUsageDescription" "音声デバイスの操作のため使用します"

echo "[plist] injected privacy keys"

# ─── 署名 ───────────────────────────────────────────────
# APPLE_SIGNING_IDENTITY が定義されていれば Developer ID で署名 (= 配布可)。
# 未定義なら従来通り adhoc 署名 (= ローカル動作のみ可、配布不可)。
SIGN_ID="${APPLE_SIGNING_IDENTITY:-}"

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

  # ─── 公証 (Notarization) ──────────────────────────────
  if [ -z "${APPLE_ID:-}" ] || [ -z "${APPLE_APP_PASSWORD:-}" ] || [ -z "${APPLE_TEAM_ID:-}" ]; then
    echo "[notarize] APPLE_ID / APPLE_APP_PASSWORD / APPLE_TEAM_ID 未設定 → 公証スキップ"
  else
    ZIP_PATH="$(mktemp -t seam-notarize.XXXXXX).zip"
    rm -f "$ZIP_PATH"
    ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"
    echo "[notarize] submitting to Apple (これに数分〜十数分かかります)..."
    xcrun notarytool submit "$ZIP_PATH" \
      --apple-id "$APPLE_ID" \
      --password "$APPLE_APP_PASSWORD" \
      --team-id "$APPLE_TEAM_ID" \
      --wait
    NOTARY_EXIT=$?
    rm -f "$ZIP_PATH"
    if [ "$NOTARY_EXIT" -ne 0 ]; then
      echo "Error: notarization failed (exit $NOTARY_EXIT)"
      exit "$NOTARY_EXIT"
    fi

    echo "[notarize] stapling..."
    xcrun stapler staple "$APP_PATH"
    xcrun stapler validate "$APP_PATH" && echo "[notarize] stapled OK"
  fi
fi

# ─── DMG 作成 ───────────────────────────────────────────
mkdir -p "$DMG_DIR"
rm -f "$DMG_PATH"

STAGE_DIR=$(mktemp -d)
trap 'rm -rf "$STAGE_DIR"' EXIT

cp -R "$APP_PATH" "$STAGE_DIR/"
ln -s /Applications "$STAGE_DIR/Applications"

hdiutil create -volname "Seam" -srcfolder "$STAGE_DIR" -ov -format UDZO "$DMG_PATH"

echo ""
echo "DMG created: $DMG_PATH"
ls -lh "$DMG_PATH"
