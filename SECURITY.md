# Security Policy

## Supported Versions

現時点では、`main` ブランチの最新状態のみをサポート対象とします。

## Reporting a Vulnerability

機密性のある脆弱性は、公開Issueではなく GitHub の Security Advisory
（Private vulnerability reporting）で報告してください。

公開Issueに投稿された場合、内容に応じてメンテナが非公開チャネルへの移動を案内します。

## Response

- 受領後、可能な範囲で再現確認を行います
- 影響度に応じて修正・リリース計画を提示します
- 修正完了後、必要に応じて公開アナウンスします

## Runtime Hardening Defaults

- バックエンドは `127.0.0.1` バインドを既定にしています
- 接続元クライアントはループバック (`127.0.0.1` / `::1`) のみ許可し、必要時のみ `server.allow_remote_clients=true` にできます
- CORS / WebSocket Origin はローカル起点のみ許可し、必要時は `server.allowed_origins` で追加できます
- `/api/*` は既定でローカル接続のみ許可し、更新系 (`POST/PUT/PATCH/DELETE`) では Origin も検証します
- `/api/debug/status` は `debug.enabled=true` のときだけ有効です
- `DELETE /api/projects/{id}?delete_output=true` は、Seam がマークした `output_dir` のみ削除します
- `/api/util/open` と `/api/util/reveal` は存在する絶対パスのみ受け付けます
