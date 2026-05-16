/**
 * HuggingFace Hub の tqdm 出力 1 行から DL 進捗を抽出。
 * 形式例:
 *   "Downloading 'pytorch_model.bin': 47%|████▋     | 235M/500M [00:32<00:36, 7.31MB/s]"
 *   "model.safetensors:  23%|██▎       | 234M/1.00G [00:15<00:51, 15.6MB/s]"
 *   "Fetching 12 files: 50%|██        | 6/12 [00:30<00:30, 2.0files/s]"
 */
export interface HFProgress {
  percent: number;   // 0-100
  current?: string;  // "234M" / "6"
  total?: string;    // "1.00G" / "12"
  speed?: string;    // "15.6MB/s"
  eta?: string;      // "00:36"
  filename?: string; // "pytorch_model.bin"
}

const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;

export function parseHFProgress(line: string | null | undefined): HFProgress | null {
  if (!line) return null;
  const clean = line.replace(ANSI, "").trim();

  // メイン: "...: 47%|...| 234M/500M [00:32<00:36, 7.31MB/s]"
  const m = clean.match(
    /(\d+)%\|[^|]*\|\s*([\d.]+\s*[A-Za-z]*)\s*\/\s*([\d.]+\s*[A-Za-z]*)\s*\[(?:[^,\]]+(?:,\s*([\d.]+\s*[A-Za-z]+\/s))?)?/,
  );
  if (m) {
    const eta = clean.match(/<\s*([\d:]+)/)?.[1];
    const filename = clean.match(/^([^:]+?):/)?.[1]?.trim();
    return {
      percent: parseInt(m[1], 10),
      current: m[2].replace(/\s+/g, ""),
      total: m[3].replace(/\s+/g, ""),
      speed: m[4]?.replace(/\s+/g, ""),
      eta,
      filename: filename && filename.length < 64 ? filename : undefined,
    };
  }

  // フォールバック: "...: 47%..."
  const m2 = clean.match(/(\d+)\s*%/);
  if (m2) {
    return { percent: parseInt(m2[1], 10) };
  }
  return null;
}

/** "235M" / "1.00G" / "512B" が「バイト量トークン」か。 */
export function isByteSizeToken(s: string | undefined | null): boolean {
  if (!s) return false;
  const token = s.trim();
  const m = token.match(/^([\d.]+)\s*([kKmMgGtT]?)([bB]?)$/);
  if (!m) return false;
  // 単位なし "2" は files カウント等の可能性が高いので bytes 扱いしない。
  return Boolean(m[2] || m[3]);
}

/** "235M" / "1.00G" / "500" → byte 数 */
export function parseSize(s: string | undefined | null): number {
  if (!s) return 0;
  const m = s.trim().match(/^([\d.]+)\s*([kKmMgGtT]?)[bB]?$/);
  if (!m) return 0;
  const v = parseFloat(m[1]);
  if (!isFinite(v) || v < 0) return 0;
  const unit = m[2].toLowerCase();
  const mul =
    unit === "k" ? 1024
    : unit === "m" ? 1024 ** 2
    : unit === "g" ? 1024 ** 3
    : unit === "t" ? 1024 ** 4
    : 1;
  return v * mul;
}

/** byte 数を人間可読に。 */
export function formatSize(bytes: number): string {
  if (!isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes.toFixed(0)} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}
