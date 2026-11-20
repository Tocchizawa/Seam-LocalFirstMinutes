"""パイプライン処理の細粒度ステージ定義。"""
from __future__ import annotations

from enum import Enum


class PipelineStage(str, Enum):
    """互換のため残す旧定義。新規実装は ``Stage`` を使う。"""
    RECORDING = "recording"
    CONVERTING_WAV = "converting_wav"
    MIXING = "mixing"
    GENERATING_MINUTES = "generating_minutes"
    SAVING = "saving"
    CLEANING_UP = "cleaning_up"
    COMPLETED = "completed"
    FAILED = "failed"


class Stage(str, Enum):
    """ユーザーに見せる細粒度ステージ。"""
    # 録音~ WAV 確定
    QUEUED = "queued"
    WAITING_RECORDING = "waiting_recording"     # 別録音中の待機
    STOPPING = "stopping"                        # recorder.stop()
    MIXING = "mixing"                            # mic + system mix
    APPLE_SPEECH = "apple_speech"
    # DB
    SAVING = "saving"
    # 要約
    SUMMARY_QUEUED = "summary_queued"
    SUMMARY_HEALTH = "summary_health"            # provider ヘルスチェック
    SUMMARY_GENERATE = "summary_generate"        # 生成 streaming
    SUMMARY_SAVE = "summary_save"                # DB 反映
    # 終端
    DONE = "done"
    SKIPPED = "skipped"                          # 要約スキップ等
    FAILED = "failed"
    CANCELLED = "cancelled"


# ユーザー向け日本語ラベル
STAGE_LABELS: dict[Stage, str] = {
    Stage.QUEUED: "キュー待機",
    Stage.WAITING_RECORDING: "録音終了待ち",
    Stage.STOPPING: "録音を停止中",
    Stage.MIXING: "音声ミックス中",
    Stage.APPLE_SPEECH: "Apple Speech で文字起こし",
    Stage.SAVING: "DB 保存中",
    Stage.SUMMARY_QUEUED: "要約キュー待機",
    Stage.SUMMARY_HEALTH: "要約サービスに接続中",
    Stage.SUMMARY_GENERATE: "要約を生成中",
    Stage.SUMMARY_SAVE: "要約を保存中",
    Stage.DONE: "完了",
    Stage.SKIPPED: "スキップ",
    Stage.FAILED: "失敗",
    Stage.CANCELLED: "キャンセル",
}


# 全体進捗バー描画用の参照順 (再文字起こし)
RETRANSCRIBE_PIPELINE: tuple[Stage, ...] = (
    Stage.QUEUED,
    Stage.APPLE_SPEECH,
    Stage.SAVING,
    Stage.DONE,
)

# 録音停止時のフロー
RECORDING_PIPELINE: tuple[Stage, ...] = (
    Stage.STOPPING,
    Stage.APPLE_SPEECH,
    Stage.SAVING,
    Stage.SUMMARY_GENERATE,
    Stage.DONE,
)
