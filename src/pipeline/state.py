"""パイプライン処理の細粒度ステージ定義。

録音停止→文字起こし→話者分離→DB保存→要約 までの一連の処理を、
ユーザーが現在地を把握できるよう細かいステップに分解する。
"""
from __future__ import annotations

from enum import Enum


class PipelineStage(str, Enum):
    """互換のため残す旧定義。新規実装は ``Stage`` を使う。"""
    RECORDING = "recording"
    FLUSHING_WHISPER = "flushing_whisper"
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
    # Whisper
    WHISPER_LOAD = "whisper_load"                # 初回 = HF DL
    WHISPER_FLUSH = "whisper_flush"              # streamer flush
    AUDIO_ANALYZE = "audio_analyze"              # VAD chunking 等
    TRANSCRIBE = "transcribe"                    # チャンク文字起こし (主要)
    # 話者分離
    DIARIZE_LOAD = "diarize_load"                # pyannote モデル読込
    DIARIZE_EXTRACT = "diarize_extract"          # 埋め込み抽出
    DIARIZE_CLUSTER = "diarize_cluster"          # クラスタリング/統合
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
    Stage.WHISPER_LOAD: "Whisperモデル準備中",
    Stage.WHISPER_FLUSH: "残り音声を文字起こし",
    Stage.AUDIO_ANALYZE: "音声を発話単位に分割",
    Stage.TRANSCRIBE: "文字起こし実行中",
    Stage.DIARIZE_LOAD: "話者分離モデル読込中",
    Stage.DIARIZE_EXTRACT: "話者特徴抽出中",
    Stage.DIARIZE_CLUSTER: "話者クラスタリング中",
    Stage.SAVING: "DB 保存中",
    Stage.SUMMARY_QUEUED: "要約キュー待機",
    Stage.SUMMARY_HEALTH: "要約 provider 確認中",
    Stage.SUMMARY_GENERATE: "要約生成中",
    Stage.SUMMARY_SAVE: "要約を保存中",
    Stage.DONE: "完了",
    Stage.SKIPPED: "スキップ",
    Stage.FAILED: "失敗",
    Stage.CANCELLED: "キャンセル",
}


# 全体進捗バー描画用の参照順 (再文字起こし)
RETRANSCRIBE_PIPELINE: tuple[Stage, ...] = (
    Stage.QUEUED,
    Stage.WHISPER_LOAD,
    Stage.AUDIO_ANALYZE,
    Stage.TRANSCRIBE,
    Stage.DIARIZE_EXTRACT,
    Stage.SAVING,
    Stage.DONE,
)

# 録音停止時のフロー
RECORDING_PIPELINE: tuple[Stage, ...] = (
    Stage.STOPPING,
    Stage.WHISPER_FLUSH,
    Stage.DIARIZE_EXTRACT,
    Stage.SAVING,
    Stage.SUMMARY_GENERATE,
    Stage.DONE,
)
