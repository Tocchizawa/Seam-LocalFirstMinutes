#!/usr/bin/env bash
set -euo pipefail

APP_PATH="gui/src-tauri/target/release/bundle/macos/Seam.app"
MACOS_BUNDLE_DIR="gui/src-tauri/target/release/bundle/macos"
UPDATER_ARCHIVE="${MACOS_BUNDLE_DIR}/Seam.app.tar.gz"
UPDATER_SIGNATURE="${UPDATER_ARCHIVE}.sig"
UPDATER_ARCHIVE_ABS="$(pwd)/${UPDATER_ARCHIVE}"
REQUIRE_NOTARIZATION="${RELEASE_UPDATER_REQUIRE_NOTARIZATION:-${RELEASE_DMG_REQUIRE_NOTARIZATION:-0}}"

if [ ! -d "$APP_PATH" ]; then
  echo "Error: app bundle not found: $APP_PATH" >&2
  exit 1
fi
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
if [ "$REQUIRE_NOTARIZATION" = "1" ]; then
  xcrun stapler validate "$APP_PATH"
fi

if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ] \
  && [ -n "${TAURI_SIGNING_PRIVATE_KEY_PATH:-}" ] \
  && [ -f "$TAURI_SIGNING_PRIVATE_KEY_PATH" ]; then
  export TAURI_SIGNING_PRIVATE_KEY="$(cat "$TAURI_SIGNING_PRIVATE_KEY_PATH")"
fi
if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ] && [ -f "$HOME/.tauri/seam-updater.key" ]; then
  export TAURI_SIGNING_PRIVATE_KEY="$(cat "$HOME/.tauri/seam-updater.key")"
fi
if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  echo "Error: TAURI_SIGNING_PRIVATE_KEY is required to sign updater artifacts" >&2
  exit 1
fi

rm -f "$UPDATER_ARCHIVE" "$UPDATER_SIGNATURE"

tar -czf "$UPDATER_ARCHIVE" -C "$MACOS_BUNDLE_DIR" "Seam.app"
TAURI_SIGNING_PRIVATE_KEY="$TAURI_SIGNING_PRIVATE_KEY" \
TAURI_SIGNING_PRIVATE_KEY_PASSWORD="${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}" \
  pnpm --dir gui tauri signer sign \
    --password "${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}" \
    "$UPDATER_ARCHIVE_ABS"

if [ ! -f "$UPDATER_SIGNATURE" ]; then
  echo "Error: updater signature not created: $UPDATER_SIGNATURE" >&2
  exit 1
fi

echo "[updater] archive: $UPDATER_ARCHIVE"
echo "[updater] signature: $UPDATER_SIGNATURE"
