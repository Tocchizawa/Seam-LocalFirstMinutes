#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-}"
TAG="${2:-}"
NOTES_FILE="${3:-}"

if [ -z "$VERSION" ]; then
  echo "Usage: $0 <version> <tag> [notes-file]" >&2
  exit 1
fi
if [ -z "$TAG" ]; then
  TAG="v${VERSION}"
fi

REPO="${GITHUB_REPOSITORY:-Tocchizawa/Seam-LocalFirstMinutes}"
MACOS_BUNDLE_DIR="gui/src-tauri/target/release/bundle/macos"
UPDATER_ARCHIVE="${MACOS_BUNDLE_DIR}/Seam.app.tar.gz"
UPDATER_SIGNATURE="${UPDATER_ARCHIVE}.sig"
OUT_DIR="gui/src-tauri/target/release/bundle/updater"
OUT_FILE="${OUT_DIR}/latest.json"

if [ ! -f "$UPDATER_ARCHIVE" ]; then
  echo "Error: updater archive not found: $UPDATER_ARCHIVE" >&2
  exit 1
fi
if [ ! -f "$UPDATER_SIGNATURE" ]; then
  echo "Error: updater signature not found: $UPDATER_SIGNATURE" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

node - "$OUT_FILE" "$VERSION" "$REPO" "$TAG" "$UPDATER_ARCHIVE" "$UPDATER_SIGNATURE" "$NOTES_FILE" <<'NODE'
const fs = require("fs");
const path = require("path");

const [
  outFile,
  version,
  repo,
  tag,
  archivePath,
  signaturePath,
  notesPath,
] = process.argv.slice(2);

const archiveName = path.basename(archivePath);
const notes = notesPath && fs.existsSync(notesPath)
  ? fs.readFileSync(notesPath, "utf8").trim()
  : "";
const signature = fs.readFileSync(signaturePath, "utf8").trim();

const data = {
  version,
  notes,
  pub_date: new Date().toISOString(),
  platforms: {
    "darwin-aarch64": {
      signature,
      url: `https://github.com/${repo}/releases/download/${tag}/${archiveName}`,
    },
  },
};

fs.writeFileSync(outFile, `${JSON.stringify(data, null, 2)}\n`);
NODE

echo "[updater] feed generated: $OUT_FILE"
