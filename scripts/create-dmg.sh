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

# ─── 既存 DMG が最新かつ公証・staple 済みなら全スキップ ─
# Apple ID をリトライで不要に notarytool に投げないための安全弁。
# .app の主バイナリより DMG が新しく、staple ticket が有効なら何もしない。
# 強制再ビルドしたければ DMG を削除して再実行する。
if [ -f "$DMG_PATH" ] \
  && [ "$DMG_PATH" -nt "$APP_PATH/Contents/MacOS/gui" ] \
  && xcrun stapler validate "$DMG_PATH" >/dev/null 2>&1; then
  echo "[skip] $DMG_PATH は最新かつ公証・staple 済みです"
  echo "       強制再生成するには DMG を削除してから再実行してください"
  ls -lh "$DMG_PATH"
  exit 0
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
fi

# ─── DMG 作成 ───────────────────────────────────────────
# Apple 公式ワークフローに合わせ、.app は staple せずに DMG に詰めてから
# DMG 自体を notarize / staple する。.app を先に staple すると macOS
# Sequoia の App Management 保護 (公証済みアプリの cross-volume 書き込み禁止)
# に hdiutil が引っかかり「Operation not permitted」で失敗する。
mkdir -p "$DMG_DIR"
rm -f "$DMG_PATH"

STAGE_DIR=$(mktemp -d)
trap 'rm -rf "$STAGE_DIR"' EXIT

cp -R "$APP_PATH" "$STAGE_DIR/"
ln -s /Applications "$STAGE_DIR/Applications"

# 重要: volname を .app のベース名 "Seam" にすると macOS Sequoia が
# /Volumes/Seam/Seam.app への書き込みを自己参照保護で拒否する
# (= "Operation not permitted")。.app 名と被らない名前を必ず使う。
hdiutil create -volname "Seam Installer" -srcfolder "$STAGE_DIR" -ov -format UDZO "$DMG_PATH"
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
elif [ -z "${APPLE_ID:-}" ] || [ -z "${APPLE_APP_PASSWORD:-}" ] || [ -z "${APPLE_TEAM_ID:-}" ]; then
  echo "[notarize] APPLE_ID / APPLE_APP_PASSWORD / APPLE_TEAM_ID 未設定 → 公証スキップ"
else
  echo "[notarize] submitting DMG to Apple (これに数分〜十数分かかります)..."
  set +e
  xcrun notarytool submit "$DMG_PATH" \
    --apple-id "$APPLE_ID" \
    --password "$APPLE_APP_PASSWORD" \
    --team-id "$APPLE_TEAM_ID" \
    --wait
  NOTARY_EXIT=$?
  set -e
  if [ "$NOTARY_EXIT" -ne 0 ]; then
    echo "Error: notarization failed (exit $NOTARY_EXIT)"
    exit "$NOTARY_EXIT"
  fi

  echo "[notarize] stapling DMG..."
  xcrun stapler staple "$DMG_PATH"
  xcrun stapler validate "$DMG_PATH" && echo "[notarize] DMG stapled OK"
fi

echo ""
echo "DMG created: $DMG_PATH"
ls -lh "$DMG_PATH"
