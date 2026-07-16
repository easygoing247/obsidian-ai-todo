"""
ObsidianAiTodo — 日付指定 ToDo リスト生成アプリ
- Obsidian の Markdown ノート群から、指定した日付に該当する未完了タスク
  （`- [ ]`）を Gemini API で動的に抽出する。
- 抽出条件:
    1) タスク行に `[date:: YYYY-MM-DD]`（期間・時間指定も可）があり、選択日と一致/期間内
    2) タスク行に `[w:: ...]`（曜日指定。英語3文字。例 `[w:: Mon,Wed,Fri]` / `[w:: Sat,Sun]`
       / `[w:: Everyday]`）があり、本日の曜日（Mon..Sun）を含む、または `Everyday` を含む
    3) タスク行に `[m:: ...]`（月次指定。数字のみ。例 `[m:: 13]` / `[m:: 1,15]`）があり、
       本日の「日」の数字と一致する
    4) タスク行に `[y:: 月,日]`（年次指定。例 `[y:: 6,11]` / `[y:: 12,25]`）があり、
       本日の「月」「日」と完全一致する（毎年1回）
- 結果は挨拶などを省いた綺麗な Markdown として st.code に出力（ワンクリックでコピー可）。

※ 隣の ../my-2nd-brain-note/app.py のノート読み込みロジックと Gemini 呼び出し方法を
   参考に、本アプリ用に独立して再構成している（あちらのファイルは一切変更しない）。
"""

import base64
import json
import os
import re
import shutil
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path

import requests
import streamlit as st
import google.generativeai as genai

# Google API のレートリミット（429 / Quota exceeded）例外。
# 環境によって import 経路が無い場合に備えてフォールバックする。
try:
    from google.api_core.exceptions import ResourceExhausted
except Exception:  # noqa: BLE001
    ResourceExhausted = None


# ---------------------------------------------------------------------------
# 設定 / シークレット（隣アプリと同じ取得方式）
# ---------------------------------------------------------------------------
def get_secret(key: str, default=None):
    """st.secrets から安全に値を取得する。

    ローカルでは `.streamlit/secrets.toml`、クラウドではアプリ設定の Secrets を
    Streamlit が自動で読み込む。未設定環境でもクラッシュしないよう default を返す。
    """
    # 1) Streamlit の Secrets（スクリプトコンテキスト内で有効）
    try:
        val = st.secrets[key]
        if val:
            return val
    except Exception:  # noqa: BLE001
        pass
    # 2) 環境変数フォールバック（バックグラウンドスレッドからも確実に参照可能）
    return os.environ.get(key, default)


GEMINI_API_KEY = get_secret("GEMINI_API_KEY")
MODEL_ID = "gemini-3.1-flash-lite"

# コンテキストに送れる総文字数の安全マージン
CHAR_LIMIT = 500_000

# 曜日 0=月曜 〜 6=日曜（date.weekday() に対応）
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]
# 曜日タグ [w:: ...] は英語3文字表記のみに限定（日本語本文の漢字との誤認を物理的に排除）
WEEKDAY_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_EVERYDAY = "Everyday"

# タイムゾーンを日本時間（JST, UTC+9）に固定する。
# サーバー/PC のローカルタイムに依存せず、常に JST で「本日」を判定する。
JST = timezone(timedelta(hours=+9))


def today_jst() -> date:
    """環境に依存せず、日本時間（JST）での本日の date を返す。"""
    return datetime.now(JST).date()

# デフォルトで参照する Vault（保管庫）のローカルパス。
# Windows の絶対パス直書きによるエスケープ事故や全角文字（「フォルダ」）の
# 文字化けを避けるため、app.py が置かれているディレクトリを基準に
# obsidian-ai-todo の絶対パスを安全に解決する。
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VAULT_PATH = os.path.abspath(os.path.join(APP_DIR, "obsidian-ai-todo"))

# 参照する Vault（保管庫）のプリセット。先頭がデフォルト選択になる。
VAULT_PRESETS = {
    "📂 obsidian-ai-todo（デフォルト）": DEFAULT_VAULT_PATH,
}
CUSTOM_PATH_LABEL = "✏️ Custom Path（自由入力）"

# プランAで生成した ToDo の保存先。ここに含まれるファイルは再抽出を防ぐため除外する。
EXCLUDED_DIR_NAME = "00_Daily_ToDo"

# FX シナリオ作成フォルダ／テンプレートファイル（平日のみ日付付きで複製）
FX_SCENARIO_DIR = "01_FX_ScenarioMaking"
FX_SCENARIO_TEMPLATE = "【朝イチ】シナリオ構築.md"

# 種ノート（Inbox）と、生成日次ファイルに引き継ぐバナー設定
INBOX_FILENAME = "ToDo_Inbox/ToDo_Inbox.md"
DEFAULT_BANNER = "morninglight.jpg"

# 生成済み ToDo を永続化するローカルキャッシュ（トークン節約＆高速表示）
CACHE_PATH = os.path.join(APP_DIR, "todo_cache.json")

# 事前フィルタ用の繰り返しタグ正規表現
_W_TAG_RE = re.compile(r"\[w::\s*(.*?)\]")
_M_TAG_RE = re.compile(r"\[m::\s*(.*?)\]")
_Y_TAG_RE = re.compile(r"\[y::\s*(.*?)\]")
# 期間指定（レンジ形式）: [date:: YYYY-MM-DD-YYYY-MM-DD ...] の開始日・終了日を両方キャプチャ
# 単一日 [date:: YYYY-MM-DD] は group(2) を持たないためマッチしない
_DATE_RANGE_RE = re.compile(
    r"\[date::\s*(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})"
)
# セーフティネット用正規表現（Gemini タスクドロップ検知＆強制救済）
_OBSIDIAN_LINK_RE = re.compile(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]")
_STRIP_ALL_TAGS_RE = re.compile(r"\[(?:date|w|m|y|priority|category)::[^\]]*\]\s*")
_TASK_MARKER_RE = re.compile(r"^\s*-\s*\[[^\]]\]\s*")
_DATE_TIME_IN_TAG_RE = re.compile(
    r"\[date::[^\]]*?(\d{2}:\d{2}(?:[-~～]\d{2}:\d{2})?)[^\]]*\]"
)
_RESCUE_CLEAN_TAG_RE = re.compile(r"\[(?:date|w|m|y|priority)::[^\]]*\]\s*")


def ensure_vault_dir(path: str) -> None:
    """Vault フォルダが存在しない場合は自動作成する（起動時の取りこぼし防止）。"""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


# デフォルト Vault は起動時に必ず存在させる
ensure_vault_dir(DEFAULT_VAULT_PATH)


# ---------------------------------------------------------------------------
# ノート読み込み（隣アプリの load_raw_notes を簡素化して流用）
# ---------------------------------------------------------------------------
def _read_raw_notes_from_disk(notes_dir: str):
    """指定ディレクトリ配下の .md を再帰的に**ディスクから直接**読み込む（無キャッシュ）。

    キャッシュ強制バイパス（朝5時自動生成・強制再生成）およびバックグラウンド
    スレッドからは、必ずこの関数を用いて最新の Inbox をゼロからパースする。
    各要素: {rel, text, chars}
    """
    if not notes_dir:
        return []

    base = Path(notes_dir)
    if not base.exists():
        return []

    notes = []
    for md in sorted(base.rglob("*.md")):
        # プランAの ToDo 保存フォルダ（00_Daily_ToDo）配下は完全に除外する。
        # パスのいずれかの階層名に一致すれば、そのファイルをスキップ。
        if EXCLUDED_DIR_NAME in md.parts:
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
        try:
            rel = str(md.relative_to(base))
        except ValueError:
            rel = md.name
        notes.append({"rel": rel, "text": text, "chars": len(text)})
    return notes


def read_notes_fresh(notes_dir: str):
    """キャッシュを介さず最新のノートを取得する（Dropbox 認証時はクラウドから直接）。

    強制再生成・朝5時ジョブなど「最新の Inbox を必ず反映したい」場面で使う。
    """
    if _dropbox_configured():
        return get_vault_storage(notes_dir).load_notes()
    return _read_raw_notes_from_disk(notes_dir)


@st.cache_data(show_spinner=False)
def load_raw_notes(notes_dir: str):
    """ノート読み込みのキャッシュ版（通常の高速表示用）。

    Dropbox 認証があればクラウドの Vault を、無ければローカルを読み込む。
    最新を強制反映したい場合は `load_raw_notes.clear()` でキャッシュ破棄、
    または `read_notes_fresh` を直接呼ぶ（キャッシュ強制バイパス）。
    """
    return read_notes_fresh(notes_dir)


@st.cache_data(show_spinner=False)
def scan_md_files(notes_dir: str):
    """デバッグ用: 除外ロジックを適用する前/後の .md 一覧を返す。

    戻り値: {"all": [相対パス...], "excluded": [相対パス...], "loaded": [相対パス...]}
      - all: フォルダ内で見つかった全 .md
      - excluded: 00_Daily_ToDo により除外された .md
      - loaded: 実際に読み込み対象になる .md
    """
    result = {"all": [], "excluded": [], "loaded": []}
    if not notes_dir:
        return result
    base = Path(notes_dir)
    if not base.exists():
        return result
    for md in sorted(base.rglob("*.md")):
        try:
            rel = str(md.relative_to(base))
        except ValueError:
            rel = md.name
        result["all"].append(rel)
        if EXCLUDED_DIR_NAME in md.parts:
            result["excluded"].append(rel)
        else:
            result["loaded"].append(rel)
    return result


def build_context(notes):
    """ノートを単一の巨大文字列に結合する。"""
    chunks = [f"--- File: {n['rel']} ---\n{n['text']}" for n in notes]
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Python 側の事前フィルタリング（Pre-filtering）
#   Gemini にテキストを渡す直前に、対象日に該当しないと「確定」できる
#   繰り返しタグ（[w::]/[m::]/[y::]）行を物理的に削除して誤抽出を構造的に防ぐ。
# ---------------------------------------------------------------------------
def _parse_int_list(s: str):
    """文字列から数字列をすべて抽出し、整数リストとして返す。

    区切り文字（カンマ「,」・ハイフン「-」・スペースなど）に依存しない。
    先頭ゼロは自動的に除去される（例: '06' → 6, '07' → 7）。
    例: '1,15' → [1, 15] / '06-27' → [6, 27] / '27' → [27]
    """
    return [int(tok) for tok in re.findall(r"\d+", s)]


# [date:: ...] タグの中身（内側）を丸ごとキャプチャ。コロン後のスペース有無/複数に対応。
_DATE_TAG_INNER_RE = re.compile(r"\[date::\s*([^\]]*)\]")
# 中身から YYYY-MM-DD パターンをすべて抽出（時間表記・スペース等の同居を完全許容）
_ISO_DATE_ANY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date_tag_verdict(line: str, target_date: date) -> str:
    """1行の `[date:: ...]` タグ群を解析し、target_date との関係を1語で返す。

    このシステムにおける日付判定の**唯一の正典（single source of truth）**。
    事前フィルタ・救済判定・最終排除フィルターのすべてがこの関数を経由する。

    【判定手順（findall 方式：時間同居・記述揺れを完全許容）】
    - `[date:: ...]` の中身から `YYYY-MM-DD` を**すべて抽出**する。
      `10:00-11:00` 等の時間表記が同居していても日付だけを確実に拾う。
    - 抽出数 2 以上 → レンジ形式: `開始日 <= today <= 終了日` を判定
    - 抽出数 1     → 単発形式: `抽出日 == today` を判定（時間同居でも日付のみ比較）
    - 抽出数 0     → ISO 日付なし（和暦・全角等）: 判定不能としてこのタグは無視

    戻り値:
      "foreign" … 今日を含まない ISO 日付/レンジが1つでもある（最優先・即ドロップ対象）
      "today"   … 今日に一致する ISO 日付/レンジがあり、foreign は1つも無い
      "none"    … 解析可能な ISO 日付が1つも無い（[date::] 不在、または和暦等の非ISO）
    """
    verdict = "none"
    for inner in _DATE_TAG_INNER_RE.findall(line):
        found = _ISO_DATE_ANY_RE.findall(inner)
        if not found:
            continue  # 非 ISO（和暦・全角等）: このタグは判定不能
        try:
            if len(found) >= 2:
                start = date.fromisoformat(found[0])
                end = date.fromisoformat(found[1])
                in_scope = start <= target_date <= end
            else:
                in_scope = date.fromisoformat(found[0]) == target_date
        except ValueError:
            continue  # 不正な日付文字列は無視
        if not in_scope:
            return "foreign"  # 今日以外が1つでもあれば即 foreign 確定
        verdict = "today"
    return verdict


def _task_excluded_for_date(line: str, target_date: date) -> bool:
    """このタスク行が対象日に該当しないことが確定しているか（=除外すべきか）を判定する。

    ホワイトリスト方式（厳格化）:
    - `[date:: ...]` に **今日以外の ISO 日付/範囲外レンジ**があれば、Gemini へ渡す前に
      物理的に除外する（単発 `[date:: 翌日]`・時間同居 `[date:: 翌日 10:00-11:00]` を含む）。
    - `[date:: ...]` が **今日の ISO 日付/期間内レンジ**なら残す。
    - `[date:: ...]` が非 ISO（和暦・全角等）なら Gemini の柔軟な解釈に委ねて残す。
    - `[w::]`（英語表記）/[m::]/[y::] の繰り返しタグを持ち、対象日に一致する行は残す。
    - 上記いずれの有効タグも持たない行、または繰り返しタグがあっても一致しない行は除外する。
    """
    weekday_en = WEEKDAY_EN[target_date.weekday()]
    day_num = target_date.day
    month_num = target_date.month

    if "[date::" in line:
        verdict = _date_tag_verdict(line, target_date)
        if verdict == "foreign":
            return True   # 今日以外の日付 → Gemini へ渡す前に100%除外
        # "today"（今日の日付）/ "none"（非ISO・Gemini に委ねる）はいずれも残す
        return False

    has_repeat = False
    matched = False

    # 曜日 [w:: ...]：英語3文字（Mon..Sun, 大文字小文字無視）または Everyday のみ
    mw = _W_TAG_RE.search(line)
    if mw:
        has_repeat = True
        tokens = [t.strip().lower() for t in mw.group(1).split(",")]
        if WEEKDAY_EVERYDAY.lower() in tokens or weekday_en.lower() in tokens:
            matched = True

    mm = _M_TAG_RE.search(line)
    if mm:
        has_repeat = True
        if day_num in _parse_int_list(mm.group(1)):
            matched = True

    my = _Y_TAG_RE.search(line)
    if my:
        has_repeat = True
        parts = _parse_int_list(my.group(1))
        if len(parts) >= 2 and parts[0] == month_num and parts[1] == day_num:
            matched = True

    if has_repeat:
        return not matched  # 繰り返しタグがあるが一致しない → 除外
    # 有効なタグ（date/w/m/y）が1つも無い → ホワイトリスト外として除外
    return True


def prefilter_text_for_date(text: str, target_date: date) -> str:
    """対象日に該当しないことが確定するタスクを、詳細メモ（子行）ごと削除する。

    ホワイトリスト化: 有効なタグを持たない（=日付設定の無い）タスクも除外する。
    レンジ形式（[date:: YYYY-MM-DD-YYYY-MM-DD]）は Python 側で期間判定し、
    期間内のタスクは翌日以降も Gemini へ渡し続ける。
    """
    lines = text.split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        is_task = stripped.startswith("- [")
        if is_task and _task_excluded_for_date(line, target_date):
            # このタスク行＋直下のインデント詳細メモ行をまとめてスキップ
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "":
                    break  # 空行で詳細メモ終了
                if not (nxt.startswith(" ") or nxt.startswith("\t")):
                    break  # インデントされていない次行で終了
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _strip_non_task_lines(text: str) -> str:
    """タスク行（`- [ ]`）とその直下のインデント子行**以外**（見出し・地の文・空行等）を除去する。

    【トークン最適化】Gemini はタスク抽出のみ行うため、本文の解説・見出し等の
    プレーンテキストは判定に不要。日付事前フィルタ通過後のタスクブロック（親行＋子行）は
    1文字も変更せず完全に保持するため、抽出精度・タスク消失防止セーフティネットには
    一切影響しない（トークン量だけを削減する）。
    """
    lines = text.split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.lstrip().startswith("- ["):
            out.append(line)
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "" or not (nxt.startswith(" ") or nxt.startswith("\t")):
                    break
                out.append(nxt)
                i += 1
            continue
        i += 1  # タスク行でない行（見出し・地の文・空行）は送信対象から除外
    return "\n".join(out)


def build_filtered_context(notes, target_date: date) -> str:
    """各ノートを対象日で事前フィルタし、タスク行以外を除去してから結合する。

    【トークン最適化（1分あたりの入力トークン上限対策）】
    1. `prefilter_text_for_date`: 対象日に関係ないタスク（過去/未来/繰り返し不一致）を
       子行ごと物理的に除去する（既存ロジック・無改変）。
    2. `_strip_non_task_lines`: 残った中からタスク行（親＋子）以外の見出し・地の文を除去。
    3. 該当タスクが1件も無いファイルは `--- File: ... ---` ヘッダーごと完全にスキップし、
       無駄なヘッダー分のトークンも節約する。
    """
    chunks = []
    for n in notes:
        filtered = prefilter_text_for_date(n["text"], target_date)
        task_only = _strip_non_task_lines(filtered)
        if not task_only.strip():
            continue  # 該当タスクが無いファイルはヘッダーごとスキップ
        chunks.append(f"--- File: {n['rel']} ---\n{task_only}")
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# セーフティネット（Gemini タスクドロップ強制検知＆救済）
#   Gemini が万が一タスクを出力から落とした場合でも、Python 側が検知して
#   デイリーノートへ強制追記することでタスクの完全消失を物理的に防ぐ。
# ---------------------------------------------------------------------------

def _strip_task_for_match(line: str) -> str:
    """マッチング用: タスク行からタグ・リンク記法・先頭マーカーを除去してコアタイトルを返す。"""
    s = _OBSIDIAN_LINK_RE.sub(r"\1", line)   # [[link|alias]] → alias
    s = _STRIP_ALL_TAGS_RE.sub("", s)         # 全メタタグ除去
    s = _TASK_MARKER_RE.sub("", s)            # - [ ] 除去
    return s.strip()


def _is_task_confirmed_for_today(line: str, target_date: date) -> bool:
    """このタスク行が target_date に確実に対応しているかを Python 側で判定する。

    【評価優先順位】[date::] タグを最優先で厳密照合し、repeat タグより先に判定する。
    これにより [date:: 翌日] [w:: Mon] のような複合タグで誤救済が起きるのを防ぐ。

    - [date:: YYYY-MM-DD-YYYY-MM-DD]: target_date が範囲内に収まる場合のみ True
    - [date:: YYYY-MM-DD]: target_date と完全一致する場合のみ True
    - [date::] が存在するが ISO 形式でない（全角・和暦等）: False（Gemini に委ねる）
    - [w::]/[m::]/[y::] のみ（[date::] なし）: 事前フィルタ通過 = 今日確定
    """
    # [date::] が存在する場合は必ず日付の厳密照合を優先する（正典 _date_tag_verdict に一元化）
    if "[date::" in line:
        # "today" のみ今日確定。"foreign"（今日以外）/"none"（非ISO）は救済対象外。
        return _date_tag_verdict(line, target_date) == "today"

    # [date::] なし: [w::]/[m::]/[y::] タグ = 事前フィルタ通過 = 今日確定
    if "[w::" in line or "[m::" in line or "[y::" in line:
        return True

    return False


def _extract_confirmed_task_blocks(context: str, target_date: date) -> list:
    """プリフィルタ済みコンテキストから「今日確定」タスクブロックを抽出する。

    各要素: (clean_title_for_match, [block_lines])
    """
    lines = context.split("\n")
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.lstrip().startswith("- [ ]"):
            block = [line]
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "" or not (nxt.startswith(" ") or nxt.startswith("\t")):
                    break
                block.append(nxt)
                i += 1
            # 親行が「今日確定」かつ、ブロック全体（子行含む）に今日以外の日付が
            # 一切含まれない場合のみ救済対象とする（複合タグの誤救済を根絶）。
            if _is_task_confirmed_for_today(line, target_date) and not _block_has_foreign_date(block, target_date):
                title = _strip_task_for_match(line)
                if len(title) >= 2:
                    blocks.append((title, block))
        else:
            i += 1
    return blocks


def _clean_parent_for_rescue(line: str) -> str:
    """救済タスクの親行を Gemini が行うはずだったクリーニングで整形する。

    - [[link|alias]] → alias に展開
    - [date:: ... HH:MM] から時間を抽出して (HH:MM) として末尾に残す
    - date/w/m/y/priority タグを除去（[category::] は保持）
    """
    s = _OBSIDIAN_LINK_RE.sub(r"\1", line)
    time_suffix = ""
    tm = _DATE_TIME_IN_TAG_RE.search(s)
    if tm:
        time_suffix = f" ({tm.group(1)})"
    s = _RESCUE_CLEAN_TAG_RE.sub("", s)
    return s.rstrip() + time_suffix


def _safety_merge_missing_tasks(gemini_output: str, confirmed_blocks: list) -> tuple:
    """Gemini 出力に漏れている「今日確定」タスクを検知し、### 優先度：低 へ強制追記する。

    戻り値: (マージ後テキスト, 救済したコアタイトルのリスト)
    漏れが無ければ元の出力と空リストをそのまま返す。
    """
    if not confirmed_blocks or not gemini_output:
        return gemini_output, []

    missing = [
        (title, block)
        for title, block in confirmed_blocks
        if title not in gemini_output
    ]

    if not missing:
        return gemini_output, []

    LOW_SECTION = "### 優先度：低"
    rescue_parts = []
    rescued_titles = []

    for title, block in missing:
        cleaned_parent = _clean_parent_for_rescue(block[0])
        rescue_parts.append("\n".join([cleaned_parent] + block[1:]))
        rescued_titles.append(title)

    rescue_text = "\n".join(rescue_parts)

    if LOW_SECTION in gemini_output:
        merged = gemini_output.rstrip() + "\n" + rescue_text
    else:
        merged = gemini_output.rstrip() + f"\n{LOW_SECTION}\n" + rescue_text

    return merged, rescued_titles


# ---------------------------------------------------------------------------
# 最終強制排除フィルター（最強の防壁）
# ---------------------------------------------------------------------------
# 優先度セクション見出し（空セクション掃除に使用）
_PRIORITY_HEADING_RE = re.compile(r"^\s*#{1,6}\s*優先度：")


def _line_has_foreign_date(line: str, target_date: date) -> bool:
    """1行の `[date:: ...]` に「今日以外」の ISO 日付/範囲外レンジがあれば True。

    判定は正典 `_date_tag_verdict` に一元化（時間同居・記述揺れを完全許容）。
    """
    return _date_tag_verdict(line, target_date) == "foreign"


def _block_has_foreign_date(block_lines, target_date: date) -> bool:
    """タスクブロック（親行＋インデント子行）のいずれかに「今日以外」の日付があれば True。"""
    return any(_line_has_foreign_date(ln, target_date) for ln in block_lines)


def _drop_empty_priority_sections(text: str) -> str:
    """タスクが1つも無くなった優先度セクション見出しを削除する（空見出し非表示の維持）。"""
    lines = text.split("\n")
    keep = [True] * len(lines)
    for idx, line in enumerate(lines):
        if _PRIORITY_HEADING_RE.match(line):
            has_task = False
            j = idx + 1
            while j < len(lines):
                if _PRIORITY_HEADING_RE.match(lines[j]):
                    break
                if lines[j].lstrip().startswith("- ["):
                    has_task = True
                    break
                j += 1
            if not has_task:
                keep[idx] = False
    return "\n".join(ln for ln, k in zip(lines, keep) if k)


def enforce_today_only_output(text: str, target_date: date) -> tuple:
    """【最終防壁】生成テキストから「今日以外の日付」を含むタスクブロックを物理排除する。

    Gemini の出力・救済マージ後の最終データに対し、ファイル書き込み直前に適用する。
    親行または子行に「today と不一致の単発 ISO 日付」または「today を含まない ISO レンジ」が
    紛れ込んでいたら、そのタスクブロックを丸ごと削除。Gemini の指示無視を100%遮断する。

    戻り値: (フィルタ後テキスト, ドロップしたタスクブロック数)
    """
    if not text:
        return text, 0

    lines = text.split("\n")
    out = []
    dropped = 0
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.lstrip().startswith("- ["):
            block = [line]
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "" or not (nxt.startswith(" ") or nxt.startswith("\t")):
                    break
                block.append(nxt)
                i += 1
            if _block_has_foreign_date(block, target_date):
                dropped += 1
                continue  # ブロックごと物理削除
            out.extend(block)
        else:
            out.append(line)
            i += 1

    filtered = "\n".join(out)
    if dropped:
        filtered = _drop_empty_priority_sections(filtered)
    return filtered, dropped


# ---------------------------------------------------------------------------
# プロンプト組み立て
# ---------------------------------------------------------------------------
def build_todo_prompt(target_date: date, weekday_jp: str, notes_context: str) -> str:
    """選択日に該当するタスクを抽出させるための指示プロンプトを組み立てる。

    曜日は Python 側（JST 基準の date）で確定済みの値を渡し、Gemini には
    日付からの曜日計算を一切させず、文字の一致だけで判定させる。
    """
    date_str = target_date.strftime("%Y-%m-%d")
    day_num = target_date.day      # 月次判定（[m:: ]）に使う「日」の数字
    month_num = target_date.month  # 年次判定（[y:: ]）に使う「月」の数字
    # Python 側で確定した曜日（日本語フル表記＋英語3文字表記）
    weekday_full = f"{weekday_jp}曜日"          # 例: "金曜日"
    weekday_en = WEEKDAY_EN[target_date.weekday()]  # 例: "Fri"
    return (
        "あなたは Obsidian ノートからタスクを抽出する正確なアシスタントです。\n"
        "以下に与えるノート全文の中から、未完了タスク（行が `- [ ]` で始まるもの）を走査し、\n"
        f"**{date_str}（{weekday_jp}曜）** に該当するタスクだけを抽出してください。\n\n"
        "【重要な前提】\n"
        "- このテキストは Python 側で**厳格に事前フィルタ済み**です。今日に関係ない曜日・"
        "月次・年次のタスクは既に物理的に除去されています。よって、渡されたタスクは基本的に"
        "すべて今日の対象です。**あなたの判断で勝手に間引いたり漏らしたりせず**、条件に沿って"
        "適切に抽出・整形してください。\n"
        "- `[w:: Everyday]` と書かれているタスクは、曜日に関わらず**無条件で100%確実に**"
        "抽出対象に含めてください（絶対に落とさないこと）。\n\n"
        "【絶対厳命：渡されたタスクの完全出力義務】\n"
        "- 以下に渡されるテキストに含まれる未完了タスクは、**本日インボックスから削除（お掃除）"
        "される単発タスク、または本日実行すべき重要な定期タスク**です。\n"
        "- 渡されたタスクは **1件も漏らさず、必ず `### 優先度：高` / `### 優先度：中` / "
        "`### 優先度：低` のいずれかのセクションに 100% 出力**しなければなりません。\n"
        "- AI の判断でタスクを省略・要約・ドロップすることは、ユーザーのタスクを消失させる"
        "**重大なシステム障害**となります。渡された親行およびそのインデント子行（詳細メモ）は"
        "機械的にすべて出力に含めてください。\n"
        "- 「重複しているから省く」「別のタスクと似ているから省く」「条件に合わないと思うから省く」"
        "などの**自律的なドロップ・圧縮・要約は完全に禁止**です。\n\n"
        "【抽出条件（いずれかを満たすタスクを対象にする）】\n"
        "1. 【曜日指定 `[w:: ...]` ／ 英語3文字・文字一致のみ・厳格】"
        f"本日の曜日は Python 側で確定済みで英語表記「{weekday_en}」（{weekday_full}）です。\n"
        "   - 曜日タグは**英語3文字表記のみ**（`Mon`,`Tue`,`Wed`,`Thu`,`Fri`,`Sat`,`Sun`、"
        "毎日は `Everyday`、大文字小文字は区別しない）です。日本語の曜日表記は使いません。\n"
        f"   - タスク行の `[w:: ...]` タグの中に、本日の「{weekday_en}」が含まれている場合、"
        "または `Everyday` が含まれている場合のみ、一致（抽出対象）とみなしてください。\n"
        "   - **あなた（AI）は日付から曜日を一切計算してはいけません。** 上で与えた"
        f"「{weekday_en}」が `[w:: ...]` に含まれるか、または `Everyday` を含むか、という"
        "文字の一致だけで厳密に判定し、含まれなければ絶対にスルーしてください。\n"
        f"   - 例: `[w:: Mon,Wed,Fri]`・`[w:: Sat,Sun]`・`[w:: {weekday_en}]`・`[w:: Everyday]` 等。"
        "タスク本文中の日本語（例「お水を買う」の『水』）を曜日と誤認しないこと。\n"
        f"2. タスク行の `[date:: ...]` の値が、選択日「{date_str}」で**始まっている、"
        f"または選択日「{date_str}」を含んでいる**場合（前方一致）。\n"
        f"   - 後ろに時間が付いた形式（例: `[date:: {date_str} 14:00]`）でも、"
        "日付部分が一致していれば必ず抽出対象とみなしてください。\n"
        f"   - 【期間指定の解釈】`[date:: ...]` の中に、波線「～」やハイフン「-」で"
        "繋がれた日付の**期間**が指定されている場合は、選択日"
        f"「{date_str}」がその**開始日から終了日までの期間内（当日を含む）**に"
        "あれば一致（抽出対象）と判定してください。\n"
        "     ・`2026-01-01～2026-01-03` のような半角表記はもちろん、"
        "`2026年1月1日～1月3日` のように全角や和暦混じり・終了側の年月省略があっても、"
        "あなたの能力で柔軟に開始日と終了日を解釈してください。\n"
        "     ・例: `- [ ] 🎍 実家に帰省する [date:: 2026年1月1日～1月3日]` の場合、"
        "選択日が `2026-01-01`／`2026-01-02`／`2026-01-03` ならすべて一致（抽出）、"
        "`2026-01-04` は期間外なので不一致（スルー）です。\n"
        "3. 【月次指定 `[m:: ...]` ／ **整数比較**・厳格】"
        f"本日の「日」は Python 側で確定済みで「{day_num}」です。\n"
        f"   - タスク行の `[m:: ...]` タグの中の数字を**整数として**解釈し、この「{day_num}」が"
        "**含まれている（カンマ区切りの場合はいずれかと一致する）**場合のみ、"
        "一致（抽出対象）とみなしてください（月の値には一切関わりません）。\n"
        "   - `[m:: ...]` の中身は「日」の数字のみです（例: `[m:: 13]`、`[m:: 1,15]`）。\n"
        "   - 【先頭ゼロは整数として扱う】`[m:: 07]` と `[m:: 7]` は同じ「毎月7日」を意味します。"
        "先頭ゼロを無視して純粋に数値として比較してください。\n"
        "   - 数字の一致だけで厳密に判定し、一致しなければ絶対にスルーしてください。\n"
        f"   - 例: 本日が「{day_num}」のとき、`[m:: {day_num}]`・`[m:: 1,{day_num}]` は"
        f"一致（抽出）、`[m:: {(day_num % 28) + 1}]` は不一致（スルー）です。\n"
        "4. 【年次指定 `[y:: 月,日]` または `[y:: 月-日]` ／ 月日の**整数**完全一致・厳格】"
        f"本日の「月」「日」は Python 側で確定済みで、月「{month_num}」・日「{day_num}」です。\n"
        f"   - タスク行の `[y:: ...]` の指定が、本日の月「{month_num}」かつ日「{day_num}」と"
        "**両方とも完全一致**する場合のみ、一致（抽出対象）とみなしてください"
        "（毎年1回の年次タスク。年には一切関わりません）。\n"
        "   - 【区切り文字】カンマ「,」とハイフン「-」の両方を区切りとして認識してください。\n"
        f"     `[y:: {month_num},{day_num}]`・`[y:: {month_num}-{day_num}]`・"
        f"`[y:: 0{month_num},{day_num}]`（先頭ゼロ付き月）はすべて同じ意味です。\n"
        "   - 【先頭ゼロは整数として扱う】`[y:: 06,27]`・`[y:: 06-27]`・`[y:: 6,27]`・`[y:: 6-27]` は"
        "すべて「毎年6月27日」を意味します。先頭ゼロを無視して純粋に数値として比較してください。\n"
        "   - **AI は年の計算や深読みを一切してはいけません。** `[y:: ...]` の1つ目の数字（月）と"
        "2つ目の数字（日）が、与えられた月日と一致するかだけで厳密に判定してください。\n"
        f"   - 例: 本日が月「{month_num}」日「{day_num}」のとき、\n"
        f"     `[y:: {month_num},{day_num}]`・`[y:: {month_num}-{day_num}]`・"
        f"`[y:: 0{month_num:02d},{day_num:02d}]` はすべて一致（抽出）。\n"
        f"     `[y:: {month_num},{(day_num % 28) + 1}]` や "
        f"`[y:: {(month_num % 12) + 1},{day_num}]` は不一致（スルー）です。\n\n"
        "【タスクブロック（親行＋インデント子行）の保持ルール（超重要）】\n"
        "- 1つのタスクは「`- [ ]` で始まる親行」＋「その直後に続く、スペース／タブで"
        "インデントされたすべての子行」を**分離不可能な1つのブロック**として扱ってください。\n"
        "- 抽出対象の親タスク行の直下にインデントされた下層情報（ツリー構造の箇条書き・"
        "サブタスク・詳細メモなど）がある場合、それらの**インデントの深さ・内容・改行を"
        "1文字も変更・省略せず、完全に維持したまま**親タスクの直下に出力に含めてください。\n"
        "- 子行の範囲は、「次のインデントされていないトップレベル行（`- [ ]` / `- [x]` など）」"
        "または「完全に空の行」が現れる直前までです。\n"
        "- **タスクのタイトル行だけを抜き出して、下層のメモを勝手にカットすることは絶対に禁止**"
        "です。箇条書き・サブチェックボックス・補足説明など、どんな内容でも無傷で拾ってください。\n"
        "- 【絶対厳守】親行（`- [ ]`）の直下にインデントされた子行（詳細メモ・箇条書き・"
        "サブタスク）は、親タスクと完全に一体の『実行手順』です。これを**1行、1文字、あるいは"
        "インデントのスペース1つであっても、省略・要約・ドロップすることは絶対に許されません。**"
        "必ずそのままの構造で100%出力に含めてください。\n"
        "- 「気を利かせて画面をスッキリさせるために詳細を省く」ような挙動は完全に禁止します。"
        "子行が10行あれば10行すべてを、元のインデント幅のまま出力してください。\n\n"
        "【優先度（priority）によるグループ分け（セクション化・最優先で適用）】\n"
        "- 各タスク行の `[priority:: 高]` / `[priority:: 中]` / `[priority:: 低]` を認識し、\n"
        "  抽出したタスクを重要度ごとに**以下の3つのセクション見出しの下に分類**してください。\n"
        "    `### 優先度：高`\n"
        "    `### 優先度：中`\n"
        "    `### 優先度：低`\n"
        "- 出力順は必ず 高 → 中 → 低 の順とし、各見出しの下に該当タスク（とその"
        "インデント子行）を配置してください。\n"
        "- `[priority:: ...]` が未指定のタスクは `### 優先度：低` のセクションに含めてください。\n"
        "- **該当タスクが1つも無いセクションは、見出しごと出力しないでください**"
        "（空の見出しを残さない）。\n"
        "- 同じ優先度セクション内のタスク同士は、元のノートの【掲載順（出現順）】を維持してください。\n"
        "- このセクション分けはカテゴリ（category）ごとの分類よりも**最優先**で適用します。\n"
        "- 各タスクに紐づく「詳細メモ（インデント子行）」は、必ずそのタスクとセットのまま"
        "同じセクションへ移動させてください。\n"
        "- 【重要】セクション見出しでグループ分けした**後**、各タスクの親行に書かれた "
        "`[priority:: 高/中/低]`（および `[優先度:…]` のような変換済み表記）のタグ文字列自体は"
        "**完全に消去（空文字に置換）**してください。重要度は見出しだけで表し、個々のタスク行には"
        "優先度の文字を一切残さないでください。\n"
        "- 出力例:\n"
        "    ### 優先度：高\n"
        "    - [ ] 📝 エッセイの推敲 (10:00)\n"
        "    ### 優先度：中\n"
        "    - [ ] 🚶 ウォーキング\n\n"
        "【厳守ルール】\n"
        "- 上記いずれの条件にも合致しないタスクは絶対に含めないでください。\n"
        "- 完了済みタスク（`- [x]`）は除外してください。\n"
        "- ノートに無い情報を創作・推測しないでください。\n"
        "- 挨拶・前置き・後書き・解説などの余計なテキストは一切出力しないでください。\n"
        "- 出力は Markdown 形式とし、各ToDoは `- [ ] 内容` の行で並べてください。\n"
        "- 詳細メモがある場合は、ToDoタイトル行の**下にインデント（半角スペース2つ以上）"
        "して**、元の階層構造を保ったまま Markdown としてそのまま出力に含めてください。\n"
        "- 【お掃除は親行のみ】以降のタグ消去・置換などの「お掃除」ルールは、"
        "**`- [ ]` で始まる親タスク行に対してのみ**適用してください。インデントされた"
        "子行（下層のツリー・メモ）のテキストやインデントは一切変更・破壊せず、無傷で残してください"
        "（子行に偶然タグ風の文字列があっても触らない）。\n"
        "- 【表記の除外】出力する各**親タスク行**のテキストからは、`[date:: ...]`、`[w:: ...]`、"
        "`[m:: ...]`、`[y:: ...]`、そして `[priority:: ...]` の表記を、大括弧 `[]` ごと完全に"
        "消去してください（前後の余分な空白も整えてください）。\n"
        "  例: `- [ ] メルマガ原稿の執筆 [date:: 2026-06-09] [category:: ワーク] [priority:: 高]`\n"
        "  → `- [ ] メルマガ原稿の執筆 [category:: ワーク]`\n"
        "- 【時間のスマート残存】ただし `[date:: ...]` の中に時間（`HH:MM` 形式）が"
        "書かれている場合（例: `[date:: 2026-06-11 14:00]`）は、メタデータ表記自体は"
        "消去しつつ、その**時間情報だけ**をタスク名の末尾に `(14:00)` のような"
        "見やすい括弧書きで残してください。\n"
        "  例: `- [ ] 来週の旅行の計画 [date:: 2026-06-11 14:00] [category:: ライフ] [priority:: 中]`\n"
        "  → `- [ ] 来週の旅行の計画 (14:00) [category:: ライフ]`\n"
        "  日付だけで時間が無い場合は、時間を残さず従来通り綺麗に消去してください。\n"
        "- 【時間範囲（タイムブロック）のスマート残存】時間が「10:00～11:00」や"
        "「10:00-11:00」のように**範囲指定**されている場合も、`[date:: ...]` タグ自体は"
        "消去しつつ、その範囲文字列をそのままタスク名の末尾に `(10:00～11:00)` のような"
        "見やすい括弧書きで残してください。区切りは「～」でも「-」でも、ユーザーが入力した"
        "範囲表記を活かして整形してください。\n"
        "  例: `- [ ] 会議の準備 [date:: 2026-06-11 10:00-11:00] [priority:: 高]`\n"
        "  → `- [ ] 会議の準備 (10:00-11:00)`\n"
        "- 【優先度の完全消去】`[priority:: 高]` / `[priority:: 中]` / `[priority:: 低]`、"
        "および過去に日本語化された `[優先度:高/中/低]` のような表記は、**大括弧ごと完全に削除**"
        "してください（画面・ファイルに優先度の文字を一切残さない）。\n"
        "  ※ただし優先度の値は**並び順（高→中→低）の決定にのみ内部的に使用**し、"
        "出力テキストには一切含めないでください。\n"
        "- `[category:: ...]` の表記は、タスクの属性として**そのまま残して**ください（消さない）。\n"
        "- 【内部リンクのプレーンテキスト化】Obsidian の内部リンク記法 `[[...]]` は、"
        "大括弧 `[[ ]]` を除去してプレーンテキストにしてください。\n"
        "  - パターンA（文字通りのリンク）: `[[ブログ下書き]]` → `ブログ下書き`\n"
        "  - パターンB（エイリアス・表示名付き）: `[[2026-06-11_カフェ巡り|お気に入りのカフェ]]`"
        " → `お気に入りのカフェ`（縦線 `|` の後ろの表示名だけを残す）\n"
        "- 上記の全お掃除ルール（内部リンク除去・日付消去&時間/時間範囲残し・優先度の完全消去）を"
        "すべて組み合わせた最終出力の例:\n"
        "  入力: `- [ ] 💡 [[エッセイ執筆ノート|エッセイの推敲]] [date:: 2026-06-11 10:00～11:00] [priority:: 高]`\n"
        "  出力: `- [ ] 💡 エッセイの推敲 (10:00～11:00)`\n"
        "- 該当タスクが1件も無い場合は、`該当するタスクはありません` とだけ出力してください。\n\n"
        "===== 以下がノートの全文です =====\n\n"
        f"{notes_context}\n\n"
        "===== ノートここまで ====="
    )


# レートリミット時のリトライ設定
RATE_LIMIT_MAX_RETRIES = 3   # 429 検知時に再試行する最大回数
RATE_LIMIT_WAIT_SECONDS = 30  # retry_delay が取得できない場合のデフォルト待機秒数

# Google API が 429 のエラーメッセージ内に埋め込む "retry_delay { seconds: NN }" を抽出
_RETRY_DELAY_RE = re.compile(r"retry_delay\s*\{\s*seconds:\s*(\d+)")


def _is_rate_limit_error(e: Exception) -> bool:
    """例外が Google API のレートリミット（429 / Quota exceeded）かどうかを判定する。"""
    if ResourceExhausted is not None and isinstance(e, ResourceExhausted):
        return True
    msg = str(e).lower()
    return any(
        kw in msg
        for kw in ("429", "quota", "resourceexhausted", "rate limit", "exceeded")
    )


def _extract_retry_delay_seconds(e: Exception) -> int:
    """429 エラーが指示する待機秒数（retry_delay）を抽出する。

    Google API のエラーメッセージ／メタデータには
    `retry_delay { seconds: NN }` が埋め込まれていることが多い。
    取得できればその秒数（+1秒の安全マージン）を、できなければ
    `RATE_LIMIT_WAIT_SECONDS`（既定30秒）を返す。
    """
    # 1) 例外オブジェクトの metadata / details から探す（google.api_core 系）
    for attr in ("metadata", "details", "args"):
        val = getattr(e, attr, None)
        if val:
            m = _RETRY_DELAY_RE.search(str(val))
            if m:
                return int(m.group(1)) + 1
    # 2) 例外の文字列表現から探す
    m = _RETRY_DELAY_RE.search(str(e))
    if m:
        return int(m.group(1)) + 1
    return RATE_LIMIT_WAIT_SECONDS


class _NoOpBox:
    """スクリプトコンテキスト外（バックグラウンドスレッド）用の st.empty 代替。"""

    def warning(self, *args, **kwargs):
        return None

    def empty(self, *args, **kwargs):
        return None


def _safe_status_box():
    """スクリプトコンテキストがあれば st.empty() を、無ければ NoOp を返す。

    Streamlit の常駐スケジューラスレッドから生成関数を呼んでも
    `missing ScriptRunContext` 警告やクラッシュを起こさないためのガード。
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        if get_script_run_ctx() is None:
            return _NoOpBox()
    except Exception:  # noqa: BLE001
        return _NoOpBox()
    try:
        return st.empty()
    except Exception:  # noqa: BLE001
        return _NoOpBox()


def generate_todo(prompt: str) -> str:
    """Gemini を呼び出して ToDo 抽出結果のテキストを返す。

    レートリミット（429 / Quota exceeded / ResourceExhausted）を検知した場合は、
    エラーが指示する `retry_delay`（無ければ既定 `RATE_LIMIT_WAIT_SECONDS` 秒）だけ
    待機して自動再試行する（最大 RATE_LIMIT_MAX_RETRIES 回）。
    成功時は案内を消し、リトライを使い切った場合は最後の例外を送出する。
    GitHub Actions 等の非対話環境でも `_safe_status_box` により安全に動作する。
    """
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(model_name=MODEL_ID)

    # 再試行の待機案内を出す/消すための差し替え可能なプレースホルダ
    # （バックグラウンドスレッド等スクリプトコンテキスト外では NoOp にフォールバック）
    warn_box = _safe_status_box()
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):  # 初回 + 最大3回の再試行
        try:
            response = model.generate_content(prompt)
            warn_box.empty()  # 成功したら待機案内を綺麗に消す
            return response.text
        except Exception as e:  # noqa: BLE001
            # レートリミットかつ再試行回数が残っている場合のみ待機して再挑戦
            if _is_rate_limit_error(e) and attempt < RATE_LIMIT_MAX_RETRIES:
                wait_s = _extract_retry_delay_seconds(e)
                warn_box.warning(
                    "⚠️ Google APIの制限に達しました。"
                    f"{wait_s}秒間待機して自動で再試行します..."
                    f"（再試行 {attempt + 1}/{RATE_LIMIT_MAX_RETRIES}）"
                )
                print(  # GitHub Actions のログにも残す
                    f"[rate-limit] 429 検知。{wait_s}秒待機して再試行 "
                    f"({attempt + 1}/{RATE_LIMIT_MAX_RETRIES})"
                )
                time.sleep(wait_s)
                continue
            # レートリミット以外、または再試行を使い切った場合は送出
            warn_box.empty()
            raise


def generate_todos_for_dates(target_dates, notes):
    """複数の日付について ToDo を1日ずつ抽出し、{ 'YYYY-MM-DD': markdown } を返す。

    日付ごとに Python 側の事前フィルタ（prefilter）を適用したクリーンな
    コンテキストを作り、日付単位で Gemini を呼び出す。
    Gemini 出力後にセーフティネット（_safety_merge_missing_tasks）を実行し、
    万が一のタスクドロップを検知して ### 優先度：低 へ強制追記する。
    """
    results = {}
    for d in target_dates:
        weekday_jp = WEEKDAY_JP[d.weekday()]
        notes_context = build_filtered_context(notes, d)
        # セーフティネット用: Gemini に渡す前に「今日確定」タスクブロックを記録
        confirmed_blocks = _extract_confirmed_task_blocks(notes_context, d)
        prompt = build_todo_prompt(d, weekday_jp, notes_context)
        try:
            raw_text = generate_todo(prompt).strip()
            # Gemini 出力を検証し、漏れたタスクを強制追記
            text, rescued = _safety_merge_missing_tasks(raw_text, confirmed_blocks)
            # 【最終防壁】今日以外の日付を含むタスクブロックを物理排除
            text, dropped = enforce_today_only_output(text, d)
            notices = []
            if rescued:
                count = len(rescued)
                names = "、".join(f"`{t[:18]}`" for t in rescued[:3])
                suffix = "…" if len(rescued) > 3 else ""
                notices.append(
                    f"> ⚠️ **自動救済 {count}件**: AIがドロップしたタスクを"
                    f"システムが強制追記しました（{names}{suffix}）"
                )
            if dropped:
                notices.append(
                    f"> 🛡️ **強制排除 {dropped}件**: 今日以外の日付が紛れ込んだタスクを"
                    "システムが物理削除しました。"
                )
            if notices:
                text = "\n".join(notices) + "\n\n" + text
        except Exception as e:  # noqa: BLE001
            text = f"（抽出中にエラーが発生しました: {e}）"
        results[d.strftime("%Y-%m-%d")] = text
    return results


# ---------------------------------------------------------------------------
# キャッシュ / Inbox / 日次ファイル のローカル入出力
# ---------------------------------------------------------------------------
def load_cache() -> dict:
    """ToDo キャッシュ（{ 'YYYY-MM-DD': markdown }）を読み込む。

    Dropbox 認証があればクラウド上（`00_Daily_ToDo/todo_cache.json`）から、
    無ければローカルの `todo_cache.json` から読み込む。
    """
    return get_vault_storage().read_cache()


def save_cache(cache: dict) -> None:
    """ToDo キャッシュを永続化する（Dropbox 認証があればクラウド、無ければローカル）。"""
    get_vault_storage().write_cache(cache)


def _parse_banner_text(text: str) -> str:
    """Inbox 本文（文字列）の先頭 YAML Frontmatter から banner の値を取り出す。

    ローカル/Dropbox どちらの読み込み経路でも共通で使えるよう、
    ファイル I/O から切り離した純粋関数として実装する。
    """
    if not text:
        return DEFAULT_BANNER
    fm = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if fm:
        bm = re.search(r"^\s*banner\s*:\s*(.+?)\s*$", fm.group(1), re.MULTILINE)
        if bm:
            return bm.group(1).strip().strip('"').strip("'") or DEFAULT_BANNER
    return DEFAULT_BANNER


def read_inbox_banner(vault_dir: str) -> str:
    """ToDo_Inbox/ToDo_Inbox.md 先頭の YAML Frontmatter から banner の値を読む（無ければ既定値）。

    Dropbox 認証があればクラウド上の Inbox から、無ければローカルから読み込む。
    """
    return get_vault_storage(vault_dir).banner()


def build_daily_file_content(body: str, banner: str) -> str:
    """日次ファイルの本文に banner を含む Frontmatter を付与する。"""
    return f"---\nbanner: {banner}\n---\n\n{body.strip()}\n"


def write_daily_file_local(vault_dir: str, date_str: str, content: str):
    """vault の 00_Daily_ToDo/ に YYYY-MM-DD.md を作成（上書き）する。

    Dropbox 認証があればクラウド上へ直接書き込み（戻り値は相対パス文字列）、
    無ければローカルへ書き込む（戻り値は Path）。
    """
    if _dropbox_configured():
        return get_vault_storage(vault_dir).write_daily(date_str, content)
    folder = Path(vault_dir) / EXCLUDED_DIR_NAME
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{date_str}.md"
    target.write_text(content, encoding="utf-8")
    return target


def _is_deletable_date_task(line: str, saved: set) -> bool:
    """date:: タスク行がインボックスから削除できる状態かを判定する。

    - レンジ形式（[date:: YYYY-MM-DD-YYYY-MM-DD]）: 保存済み日付の最大値が終了日以上に
      なって初めて削除対象とする（期間の途中は残す）。
    - 単一日指定（[date:: YYYY-MM-DD ...]）: 保存済み日付文字列が行内に含まれれば削除
      （従来通り）。
    """
    rm = _DATE_RANGE_RE.search(line)
    if rm:
        end_date_str = rm.group(2)
        # saved の最大値が終了日以上 → 期間完了として削除
        return bool(saved) and max(saved) >= end_date_str
    # 単一日：保存済み日付が行中に部分一致すれば削除
    return any(ds in line for ds in saved)


def copy_fx_scenario_template(vault_dir: str, target_date: date):
    """平日（月〜金）の場合のみ FX シナリオテンプレートを日付付きで複製する。

    - 土曜・日曜はスキップ（FX 市場が閉まっているため）。
    - コピー先が既に存在する場合は上書きせずスキップ（冪等）。
    - テンプレートが見つからない場合はクラッシュせず戻り値でエラーを返す。

    戻り値: (実行結果フラグ, メッセージ文字列)
      "weekend"       → 土日のためスキップ
      "already_exists"→ 既に存在するためスキップ
      "no_template"   → テンプレートファイルが見つからない
      "error:..."     → OS エラー
      それ以外（True, dest_path_str）→ 複製成功
    """
    # Dropbox 認証があればクラウド経路（土日スキップ等の判定は同一ロジック）
    if _dropbox_configured():
        return get_vault_storage(vault_dir).copy_fx_scenario(target_date)

    # 土曜(5)・日曜(6) はスキップ
    if target_date.weekday() >= 5:
        return False, "weekend"

    fx_dir = Path(vault_dir) / FX_SCENARIO_DIR
    template_path = fx_dir / FX_SCENARIO_TEMPLATE

    if not template_path.exists():
        return False, "no_template"

    date_prefix = target_date.strftime("%y%m%d")  # 例: "260626"
    dest_path = fx_dir / f"{date_prefix}{FX_SCENARIO_TEMPLATE}"

    if dest_path.exists():
        return False, "already_exists"

    try:
        shutil.copy2(template_path, dest_path)
        return True, str(dest_path)
    except OSError as e:
        return False, f"error: {e}"


def archive_past_files(vault_dir: str, target_date: date) -> dict:
    """月初め（1日）に前月ファイルを月次フォルダへ移動する。
    1月1日の場合はさらに前年の月次フォルダを西暦フォルダへ年次アーカイブする。

    動作条件: target_date.day == 1 のみ。それ以外は空辞書を即返し。

    戻り値:
        {
            "monthly_folder": "YYYY-MM",
            "monthly_daily":  移動ファイル数（00_Daily_ToDo）,
            "monthly_fx":     移動ファイル数（01_FX_ScenarioMaking）,
            "yearly_folder":  "YYYY" | None,
            "yearly_daily":   移動フォルダ数 | None,
            "yearly_fx":      移動フォルダ数 | None,
        }
    """
    # Dropbox 認証があればクラウド経路（move API でフォルダ整理）
    if _dropbox_configured():
        return get_vault_storage(vault_dir).archive(target_date)

    if target_date.day != 1:
        return {}

    # 前月の年・月を算出
    if target_date.month == 1:
        prev_year, prev_month = target_date.year - 1, 12
    else:
        prev_year, prev_month = target_date.year, target_date.month - 1

    prev_month_str = f"{prev_year}-{prev_month:02d}"           # e.g. "2026-06"
    fx_prefix      = f"{str(prev_year)[-2:]}{prev_month:02d}"  # e.g. "2606"

    daily_base = Path(vault_dir) / EXCLUDED_DIR_NAME
    fx_base    = Path(vault_dir) / FX_SCENARIO_DIR

    def _move_files(src_dir: Path, dest_dir: Path, pattern: str) -> int:
        if not src_dir.exists():
            return 0
        dest_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for f in sorted(src_dir.glob(pattern)):
            if f.is_file():
                try:
                    shutil.move(str(f), str(dest_dir / f.name))
                    moved += 1
                except OSError:
                    pass
        return moved

    def _move_dirs(src_dir: Path, dest_dir: Path, names: list) -> int:
        if not src_dir.exists():
            return 0
        dest_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for name in names:
            src_sub = src_dir / name
            if src_sub.exists() and src_sub.is_dir():
                try:
                    shutil.move(str(src_sub), str(dest_dir / name))
                    moved += 1
                except OSError:
                    pass
        return moved

    # ① 月次アーカイブ
    monthly_daily = _move_files(
        daily_base,
        daily_base / prev_month_str,
        f"{prev_month_str}-*.md",
    )
    monthly_fx = _move_files(
        fx_base,
        fx_base / prev_month_str,
        f"{fx_prefix}*.md",
    )

    result: dict = {
        "monthly_folder": prev_month_str,
        "monthly_daily":  monthly_daily,
        "monthly_fx":     monthly_fx,
        "yearly_folder":  None,
        "yearly_daily":   None,
        "yearly_fx":      None,
    }

    # ② 年次アーカイブ（1月1日のみ：前年月次フォルダを西暦フォルダへ格納）
    if target_date.month == 1:
        prev_year_str   = str(prev_year)
        month_dir_names = [f"{prev_year}-{m:02d}" for m in range(1, 13)]

        yearly_daily = _move_dirs(
            daily_base,
            daily_base / prev_year_str,
            month_dir_names,
        )
        yearly_fx = _move_dirs(
            fx_base,
            fx_base / prev_year_str,
            month_dir_names,
        )
        result.update({
            "yearly_folder": prev_year_str,
            "yearly_daily":  yearly_daily,
            "yearly_fx":     yearly_fx,
        })

    return result


def cleanup_inbox_singles(vault_dir: str, saved_date_strs) -> int:
    """`## 単発` 見出し配下で `[date:: ...]` を持つタスク行のうち、
    保存済み日付（saved_date_strs のいずれか）に対応するものを Inbox から削除する。

    詳細メモ（インデント行）もセットで削除。安全のため書き換え前に .bak を作成。
    削除したタスクは、同じフォルダ内の `ToDo_Inbox_Done.md` へ元のカテゴリ別に
    転記（アーカイブ）してから Inbox を書き換える。戻り値: 削除したタスク数。

    【重要な設計上の保証】
    - `[date::]` タグを持たないタスク（＝ `[w::]`/`[m::]`/`[y::]` のみの定期タスク）は
      条件 `"[date::" in line` を満たさないため、このクリーンアップ処理の対象外となる。
      定期タスクは次月・次年以降も繰り返し使用されるため、Inbox から削除されることはない。
    - 期間指定タスク（`[date:: YYYY-MM-DD-YYYY-MM-DD]`）は終了日の保存まで削除されない
      （`_is_deletable_date_task` が終了日以降のみ True を返す設計による）。
    """
    # Dropbox 認証があればクラウド上の Inbox を直接お掃除（.bak も Dropbox 上に作成）
    if _dropbox_configured():
        removed = get_vault_storage(vault_dir).cleanup_inbox(saved_date_strs)
        if removed:
            load_raw_notes.clear()
            scan_md_files.clear()
        return removed

    path = Path(vault_dir) / INBOX_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0

    new_text, removed, removed_blocks = _compute_inbox_cleanup(text, saved_date_strs)

    if removed:
        try:
            shutil.copy2(path, str(path) + ".bak")  # 安全のためバックアップ
        except OSError:
            pass
        path.write_text(new_text, encoding="utf-8")
        get_vault_storage(vault_dir)._record_done_tasks(INBOX_FILENAME, removed_blocks)
        load_raw_notes.clear()
        scan_md_files.clear()
    return removed


def _compute_inbox_cleanup(text: str, saved_date_strs) -> tuple:
    """Inbox 本文（文字列）から、保存済み日付に対応する `## 単発` の日付付きタスクを
    詳細メモ（インデント行）ごと除去した新テキストを返す純粋関数。

    ファイル I/O から切り離すことで、ローカル/Dropbox どちらの書き込み経路でも
    同一の「範囲保持お掃除ロジック（_is_deletable_date_task）」を再利用できる。
    戻り値: (お掃除後テキスト, 削除タスク数, 削除ブロック一覧)
      削除ブロック一覧の各要素は (category_name, [block_lines]) のタプル。
      category_name は削除時点で通過していた既知の見出し名（例: "単発"）で、
      ToDo_Inbox_Done.md への転記先セクションの判定に使う。
    """
    saved = set(saved_date_strs)
    lines = text.split("\n")
    out = []
    in_singles = False
    current_category = None  # 直近に通過した既知カテゴリ見出し（##/### どちらでも可）
    removed = 0
    removed_blocks = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("#"):
            if stripped.startswith("## "):
                in_singles = stripped == "## 単発"
            cat = _classify_done_heading(line)
            if cat is not None:
                current_category = cat
            out.append(line)
            i += 1
            continue
        is_task = line.lstrip().startswith("- [")
        if (
            in_singles
            and is_task
            and "[date::" in line
            and _is_deletable_date_task(line, saved)
        ):
            # この行＋直下インデント詳細メモ行をまとめて削除
            block = [line]
            removed += 1
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "" or not (nxt.startswith(" ") or nxt.startswith("\t")):
                    break
                block.append(nxt)
                i += 1
            removed_blocks.append((current_category or "単発", block))
            continue
        out.append(line)
        i += 1

    return "\n".join(out), removed, removed_blocks


# ---------------------------------------------------------------------------
# ToDo_Inbox_Done.md への転記（削除タスクのカテゴリ別アーカイブ）
# ---------------------------------------------------------------------------
DONE_BASENAME = "ToDo_Inbox_Done.md"
# ToDo_Inbox.md の見出し構造（## 定期 > ### 毎日/毎週/毎月/毎年、## 今だけ.../## 単発）に
# 対応する Done ファイル側の正典カテゴリ名（Done 側はすべて `##` レベルに平坦化する）。
DONE_HEADINGS_ORDER = ["毎日", "毎週（曜日指定）", "毎月", "毎年", "今だけ毎日意識すること", "単発"]
_DONE_CATEGORY_NAMES = set(DONE_HEADINGS_ORDER)
_DONE_HEADING_TEXT_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$")


def _classify_done_heading(line: str):
    """見出し行が Done ファイルの既知カテゴリ名に一致すればそのカテゴリ名を返す。

    `##`/`###` などレベルを問わず、見出しテキストの完全一致で判定する
    （例: `### 毎日` も `## 毎日` も同じ "毎日" として扱う）。
    一致しない見出し（`## 定期`, `## 記述方法` 等）は None を返す。
    """
    m = _DONE_HEADING_TEXT_RE.match(line.strip())
    if not m:
        return None
    text = m.group(1).strip()
    return text if text in _DONE_CATEGORY_NAMES else None


def _done_path_for(inbox_rel: str) -> str:
    """Inbox の実相対パスと同じフォルダ内にある Done ファイルの相対パスを返す。"""
    inbox_rel = (inbox_rel or "").replace("\\", "/")
    if "/" in inbox_rel:
        return inbox_rel.rsplit("/", 1)[0] + "/" + DONE_BASENAME
    return DONE_BASENAME


def _default_done_template() -> str:
    """ToDo_Inbox_Done.md が存在しない場合の初期テンプレート（既知の見出しのみ）。"""
    return "\n\n".join(f"## {name}" for name in DONE_HEADINGS_ORDER) + "\n"


def _compute_done_append(existing_done_text: str, removed_blocks: list) -> str:
    """削除されたタスクブロックを、カテゴリごとに Done ファイルの該当見出し直後へ
    挿入した新テキストを返す純粋関数。

    - 該当する `## <カテゴリ名>` 見出しが既にあれば、その【見出しの直後（先頭）】に
      挿入する。新しく転記されたタスクほど上に来るため、日付の新しい順に並ぶ
      （＝古いタスクほど下にずれていく）。
    - 見出しが無ければ、末尾に見出しごと新設して追記する（この場合は必然的に
      見出し直後＝唯一のタスクなので位置の違いは生じない）。
    - Done ファイルが未作成/空の場合は、既知の6見出しからなる初期テンプレートを土台にする。
    - 他の見出し・その配下の既存タスクの順序には一切手を加えない。
    """
    if not removed_blocks:
        return existing_done_text

    text = existing_done_text if existing_done_text and existing_done_text.strip() else _default_done_template()
    lines = text.split("\n")

    # カテゴリごとにブロックをグルーピング（渡された順序を維持）
    grouped = {}
    for category, block in removed_blocks:
        grouped.setdefault(category, []).append(block)

    for category, blocks in grouped.items():
        heading = f"## {category}"
        insert_lines = []
        for block in blocks:
            insert_lines.extend(block)

        start = None
        for idx, l in enumerate(lines):
            if l.strip() == heading:
                start = idx
                break

        if start is None:
            # 見出しが無ければ末尾に見出しごと新設
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append(heading)
            lines.extend(insert_lines)
        else:
            # 見出し行の直後（そのセクションの先頭）に挿入する
            lines = lines[:start + 1] + insert_lines + lines[start + 1:]

    return "\n".join(lines)


_CATEGORY_RE = re.compile(r"\[category::\s*(.*?)\]")
_DATE_YMD_RE = re.compile(r"\[date::\s*(\d{4}-\d{2}-\d{2})")
_PRIORITY_RE = re.compile(r"\[priority::\s*(.*?)\]")
_HEADING_RE = re.compile(r"^#{1,6}\s")

# 優先度の高さ（降順ソート用）: 高 > 中 > 低 > 指定なし
_PRIORITY_RANK = {"高": 3, "中": 2, "低": 1}


def _priority_rank(text: str) -> int:
    """タスク行/優先度文字列から優先度ランクを返す（高3/中2/低1/なし0）。"""
    m = _PRIORITY_RE.search(text)
    val = m.group(1).strip() if m else (text or "").strip()
    return _PRIORITY_RANK.get(val, 0)


def _parse_categories_text(text: str):
    """Inbox 本文から `[category:: ...]` の中身を重複なく抽出する（出現順）。純粋関数。"""
    seen = []
    for raw in _CATEGORY_RE.findall(text or ""):
        v = raw.strip()
        if v and v not in seen:
            seen.append(v)
    return seen


def extract_categories(vault_dir: str):
    """ToDo_Inbox/ToDo_Inbox.md 全体から `[category:: ...]` を抽出（Dropbox 認証時はクラウド）。"""
    return get_vault_storage(vault_dir).extract_categories()


def _compute_inbox_append(existing: str, title: str, date_val, category: str, priority: str) -> str:
    """Inbox 本文（文字列）に新 ToDo 行を `## 単発` 内へ日付昇順で挿入した新テキストを返す。

    ファイル I/O から切り離した純粋関数。ローカル/Dropbox どちらの書き込み経路でも
    同一の挿入ロジック（日付昇順＋同日内は優先度降順）を再利用する。
    """
    parts = [f"- [ ] {title.strip()}"]
    if date_val:
        parts.append(f"[date:: {date_val}]")
    if category and category.strip():
        parts.append(f"[category:: {category.strip()}]")
    if priority and priority != "未設定":
        parts.append(f"[priority:: {priority}]")
    new_line = " ".join(parts)

    lines = (existing or "").split("\n")

    # ① `## 単発` 見出しを特定
    start = None
    for idx, l in enumerate(lines):
        if l.strip() == "## 単発":
            start = idx
            break

    if start is None:
        # 見出しが無ければ末尾に見出しごと追加
        return "\n".join(lines + ["", "## 単発", new_line])

    # ② 次の見出し or 末尾までを単発エリアとする
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if _HEADING_RE.match(lines[idx]):
            end = idx
            break

    # ③④ エリア内の既存タスクと比較し、(日付 昇順, 同日内は優先度 降順) で
    #     収まる位置を特定する。新タスクは「自分より後ろに来るべき最初の既存
    #     タスク」の直前に挿入する（後ろ条件: date > 新date または 同date かつ rank < 新rank）。
    insert_at = end  # 既定: エリア末尾（次の見出しの直前）
    if date_val:
        new_rank = _priority_rank(priority or "")
        _dm = re.match(r"(\d{4}-\d{2}-\d{2})", str(date_val))
        new_date_str = _dm.group(1) if _dm else str(date_val)
        for idx in range(start + 1, end):
            if not lines[idx].lstrip().startswith("- ["):
                continue
            m = _DATE_YMD_RE.search(lines[idx])
            if not m:
                continue  # 日付なしタスクは比較対象外
            existing_date = m.group(1)
            if existing_date > new_date_str:
                insert_at = idx
                break
            if existing_date == new_date_str and _priority_rank(lines[idx]) < new_rank:
                insert_at = idx
                break
    new_lines = lines[:insert_at] + [new_line] + lines[insert_at:]
    return "\n".join(new_lines)


def append_task_to_inbox(vault_dir: str, title: str, date_val, category: str, priority: str) -> bool:
    """新構文に準拠した ToDo 行を `## 単発` セクション内に日付昇順で挿入する。

    Dropbox 認証が環境にある場合は Dropbox 上の Inbox を直接読み書きし、
    無ければ従来どおりローカルの Inbox を更新する。いずれも書き換え前に `.bak` を作成。
    """
    # --- クラウド（Dropbox）経路 ---
    if _dropbox_configured():
        storage = get_vault_storage(vault_dir)
        ok = storage.append_task(title, date_val, category, priority)
        if ok:
            load_raw_notes.clear()
            scan_md_files.clear()
        return ok

    # --- ローカル経路（従来動作）---
    path = Path(vault_dir) / INBOX_FILENAME
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return False

    new_text = _compute_inbox_append(existing, title, date_val, category, priority)

    try:
        if path.exists():
            shutil.copy2(path, str(path) + ".bak")  # 安全のためバックアップ
        path.write_text(new_text, encoding="utf-8")
        load_raw_notes.clear()
        scan_md_files.clear()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# GitHub への保存（Create or Update）
# ---------------------------------------------------------------------------
def push_to_github(file_path: str, content: str, commit_message: str):
    """GitHub REST API で file_path にファイルをコミットする。

    既存ファイルがあれば sha を取得してから PUT で上書き、無ければ新規作成する。
    認証情報は st.secrets から取得する。
    戻り値: (成功フラグ bool, メッセージ str)
    """
    token = get_secret("GITHUB_TOKEN")
    repo = get_secret("GITHUB_REPO")      # 形式: owner/repo
    branch = get_secret("GITHUB_BRANCH")

    missing = [
        name
        for name, val in (
            ("GITHUB_TOKEN", token),
            ("GITHUB_REPO", repo),
            ("GITHUB_BRANCH", branch),
        )
        if not val
    ]
    if missing:
        return False, (
            "次の Secrets が未設定です: "
            + ", ".join(f"`{m}`" for m in missing)
            + "。`.streamlit/secrets.toml` を確認してください。"
        )

    api_url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        # 1) 既存ファイルの sha を取得（あれば上書き、無ければ新規作成）
        sha = None
        get_res = requests.get(
            api_url, headers=headers, params={"ref": branch}, timeout=30
        )
        if get_res.status_code == 200:
            sha = get_res.json().get("sha")
        elif get_res.status_code not in (404,):
            return False, (
                f"既存ファイル確認でエラー (HTTP {get_res.status_code}): "
                f"{get_res.json().get('message', get_res.text)}"
            )

        # 2) PUT でコミット（content は base64 エンコードが必須）
        payload = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        put_res = requests.put(api_url, headers=headers, json=payload, timeout=30)
        if put_res.status_code in (200, 201):
            action = "更新" if sha else "新規作成"
            return True, f"{file_path} を{action}しました。"
        return False, (
            f"Push に失敗しました (HTTP {put_res.status_code}): "
            f"{put_res.json().get('message', put_res.text)}"
        )
    except requests.RequestException as e:
        return False, f"通信エラー: {e}"


# ---------------------------------------------------------------------------
# Vault ストレージ抽象化（ローカルディスク / Dropbox API を透過的に切替）
#   - 日付判定の正典 `_date_tag_verdict` や生成ロジックは一切 file I/O を持たないため、
#     ここで「読み書きの物理層」だけを差し替えれば、PC OFF でもクラウド完結で動作する。
# ---------------------------------------------------------------------------
# Dropbox 上でキャッシュ（todo_cache.json）を保存する相対パス
# （00_Daily_ToDo 配下＝ノート読込の除外対象なので、Obsidian のノートには混ざらない）
DROPBOX_CACHE_REL = f"{EXCLUDED_DIR_NAME}/todo_cache.json"


def _dropbox_configured() -> bool:
    """環境に Dropbox 認証情報があるか（アクセストークン単体、または Refresh 一式）。"""
    if get_secret("DROPBOX_ACCESS_TOKEN"):
        return True
    return bool(
        get_secret("DROPBOX_REFRESH_TOKEN")
        and get_secret("DROPBOX_APP_KEY")
        and get_secret("DROPBOX_APP_SECRET")
    )


class _VaultStorageBase:
    """ローカル/Dropbox 共通の高水準操作（read_text/write_text/make_backup を基に実装）。

    ここに置いた操作は物理層（read_text 等）に依存せず、純粋ロジック関数を再利用する。
    """

    def _resolve_rel(self, rel: str) -> str:
        """相対パスの実体解決。既定ではそのまま返す。

        Dropbox 実装では、`self.base`（DROPBOX_VAULT_PATH）が実際の Vault
        ルートと厳密に一致しない場合の自己修復ロジックをここでオーバーライドする。
        """
        return rel

    def banner(self) -> str:
        return _parse_banner_text(self.read_text(self._resolve_rel(INBOX_FILENAME)) or "")

    def extract_categories(self):
        return _parse_categories_text(self.read_text(self._resolve_rel(INBOX_FILENAME)) or "")

    def append_task(self, title, date_val, category, priority) -> bool:
        rel = self._resolve_rel(INBOX_FILENAME)
        existing = self.read_text(rel) or ""
        new_text = _compute_inbox_append(existing, title, date_val, category, priority)
        try:
            self.make_backup(rel)  # 書き換え前に .bak を作成
            self.write_text(rel, new_text)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _record_done_tasks(self, inbox_rel: str, removed_blocks: list) -> None:
        """Inbox から削除されたタスクブロックを、同じフォルダの ToDo_Inbox_Done.md へ
        カテゴリ別に追記する（Local/Dropbox 共通・read_text/write_text/make_backup
        経由なので物理層に依存しない）。

        転記に失敗しても Inbox 側の削除は既に完了しているため、ここでは例外を
        飲み込んで処理を継続する（Done への転記漏れは致命的ではない）。
        """
        if not removed_blocks:
            return
        done_rel = _done_path_for(inbox_rel)
        try:
            existing = self.read_text(done_rel) or ""
            new_text = _compute_done_append(existing, removed_blocks)
            if existing:
                self.make_backup(done_rel)
            self.write_text(done_rel, new_text)
        except Exception:  # noqa: BLE001
            pass


class LocalStorage(_VaultStorageBase):
    """ローカルファイルシステム上の Vault（従来動作）。既存の実績関数へ委譲する。"""

    kind = "local"

    def __init__(self, root: str):
        self.root = root

    def load_notes(self):
        return _read_raw_notes_from_disk(self.root)

    def read_text(self, rel: str):
        try:
            return (Path(self.root) / rel).read_text(encoding="utf-8")
        except OSError:
            return None

    def write_text(self, rel: str, content: str):
        p = Path(self.root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def make_backup(self, rel: str):
        src = Path(self.root) / rel
        try:
            shutil.copy2(src, str(src) + ".bak")
        except OSError:
            pass

    def cleanup_inbox(self, saved_date_strs):
        return cleanup_inbox_singles(self.root, saved_date_strs)

    def copy_fx_scenario(self, target_date):
        return copy_fx_scenario_template(self.root, target_date)

    def archive(self, target_date):
        return archive_past_files(self.root, target_date)

    def write_daily(self, date_str: str, content: str) -> str:
        return str(write_daily_file_local(self.root, date_str, content))

    def read_cache(self) -> dict:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_cache(self, cache: dict) -> None:
        try:
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


class DropboxStorage(_VaultStorageBase):
    """Dropbox 上の Vault を HTTP API 経由で直接読み書きする（PC OFF でもクラウド完結）。

    認証は OAuth2。App Key / App Secret / Refresh Token があれば実行時に短命の
    アクセストークンへ自動リフレッシュする（トークン期限切れを回避）。
    `DROPBOX_ACCESS_TOKEN` 単体が与えられた場合はそれを直接使う。
    Vault のルートは `DROPBOX_VAULT_PATH`（例: "/Apps/remotely-save/MyVault"）。
    """

    kind = "dropbox"
    _CONTENT = "https://content.dropboxapi.com/2"
    _RPC = "https://api.dropboxapi.com/2"

    def __init__(self):
        base = (get_secret("DROPBOX_VAULT_PATH") or "").strip().rstrip("/")
        # 【パス正規化】先頭スラッシュが無い場合は自動補完する（Dropbox API はパスが
        # "/" で始まるか完全な空文字列であることを要求するため、"Apps/xxx" のような
        # 書き方でも malformed_path エラーにならず動作するようにする）。
        if base and not base.startswith("/"):
            base = "/" + base
        self.base = base
        self._access = get_secret("DROPBOX_ACCESS_TOKEN")
        self._app_key = get_secret("DROPBOX_APP_KEY")
        self._app_secret = get_secret("DROPBOX_APP_SECRET")
        self._refresh = get_secret("DROPBOX_REFRESH_TOKEN")
        self._token = None
        self._token_expiry = 0.0  # epoch 秒。期限が近づいたら自動で再取得する
        # 直近の list_folder で 409（path/not_found）が発生した場合の詳細（UI診断用）
        self.last_list_error = None
        # INBOX_FILENAME の実体解決結果のメモ化（self.base 不一致への自己修復キャッシュ）
        self._inbox_rel_cache = None

    # -- 認証 --------------------------------------------------------------
    def _bearer(self) -> str:
        if self._refresh and self._app_key and self._app_secret:
            # 期限まで60秒以上あるキャッシュ済みトークンはそのまま再利用する
            if self._token and time.time() < self._token_expiry - 60:
                return self._token
            r = requests.post(
                "https://api.dropbox.com/oauth2/token",
                data={"grant_type": "refresh_token", "refresh_token": self._refresh},
                auth=(self._app_key, self._app_secret),
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            self._token = data["access_token"]
            # expires_in（既定4時間）に基づき失効時刻を記録
            self._token_expiry = time.time() + int(data.get("expires_in", 14400))
            return self._token
        if not self._access:
            raise RuntimeError("Dropbox 認証情報が不足しています。")
        return self._access

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self._bearer()}"}
        if extra:
            h.update(extra)
        return h

    def _abs(self, rel: str) -> str:
        rel = (rel or "").replace("\\", "/").lstrip("/")
        return f"{self.base}/{rel}" if self.base else f"/{rel}"

    # -- 基本 I/O ----------------------------------------------------------
    def read_text(self, rel: str):
        # Dropbox-API-Arg は ASCII 必須のため ensure_ascii=True（既定）で \uXXXX 化する
        arg = json.dumps({"path": self._abs(rel)})
        r = requests.post(
            f"{self._CONTENT}/files/download",
            headers=self._headers({"Dropbox-API-Arg": arg}),
            timeout=60,
        )
        if r.status_code == 200:
            return r.content.decode("utf-8", errors="ignore")
        if r.status_code == 409:  # not_found
            return None
        r.raise_for_status()

    def write_text(self, rel: str, content: str):
        arg = json.dumps(
            {"path": self._abs(rel), "mode": "overwrite", "autorename": False, "mute": True}
        )
        r = requests.post(
            f"{self._CONTENT}/files/upload",
            headers=self._headers(
                {"Dropbox-API-Arg": arg, "Content-Type": "application/octet-stream"}
            ),
            data=content.encode("utf-8"),
            timeout=60,
        )
        r.raise_for_status()

    def exists(self, rel: str) -> bool:
        r = requests.post(
            f"{self._RPC}/files/get_metadata",
            headers=self._headers(),
            json={"path": self._abs(rel)},
            timeout=30,
        )
        return r.status_code == 200

    def make_backup(self, rel: str):
        txt = self.read_text(rel)
        if txt is not None:
            self.write_text(rel + ".bak", txt)

    def _move(self, src_rel: str, dst_rel: str) -> bool:
        r = requests.post(
            f"{self._RPC}/files/move_v2",
            headers=self._headers(),
            json={
                "from_path": self._abs(src_rel),
                "to_path": self._abs(dst_rel),
                "autorename": False,
            },
            timeout=30,
        )
        return r.status_code == 200

    def _list_folder(self, rel: str, recursive: bool = False):
        # ルート（base 直下）を列挙する場合、Dropbox では path="" を渡す必要がある
        path = self._abs(rel) if rel else (self.base or "")
        entries = []
        r = requests.post(
            f"{self._RPC}/files/list_folder",
            headers=self._headers(),
            json={"path": path, "recursive": recursive, "limit": 2000},
            timeout=60,
        )
        if r.status_code == 409:  # フォルダが無い（path/not_found 等）
            # 【重要】ここで黙って空リストを返すと「パス設定ミス」と「本当に空」の
            # 区別がUI側で一切できなくなるため、詳細をインスタンスに記録しておく。
            try:
                detail = r.json()
            except ValueError:
                detail = {"error_summary": r.text}
            self.last_list_error = {"path": path, "status": 409, "detail": detail}
            return entries
        r.raise_for_status()
        self.last_list_error = None  # 成功したので直前のエラー記録はクリア
        data = r.json()
        entries.extend(data.get("entries", []))
        while data.get("has_more"):
            r = requests.post(
                f"{self._RPC}/files/list_folder/continue",
                headers=self._headers(),
                json={"cursor": data["cursor"]},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            entries.extend(data.get("entries", []))
        return entries

    def _rel_of(self, path_display: str) -> str:
        """Dropbox の絶対 path_display を Vault ルート相対に変換する。"""
        if self.base and path_display.lower().startswith(self.base.lower()):
            return path_display[len(self.base):].lstrip("/")
        return path_display.lstrip("/")

    def _resolve_rel(self, rel: str) -> str:
        """Inbox（INBOX_FILENAME）の実体パスを自己修復的に解決する。

        【背景】`self.base`（DROPBOX_VAULT_PATH）が実際の Vault ルートと厳密に一致しない
        場合、`self.base + INBOX_FILENAME` の直接パス GET/PUT は 409（path/not_found）で
        静かに失敗する。一方 `load_notes()` は再帰列挙のため `self.base` の多少のズレに
        関わらず .md ファイルを発見できてしまい、「タスク読み込みは成功するのに
        カテゴリ／バナー／新規追加だけ空になる」という非対称な症状を生む。
        これを避けるため、直接パスが失敗した場合は再帰列挙から Inbox を探し出し、
        見つかった実際の相対パスを以降ずっと使い回す（インスタンス内でメモ化）。
        """
        if rel != INBOX_FILENAME:
            return rel
        if self._inbox_rel_cache is not None:
            return self._inbox_rel_cache
        if self.exists(INBOX_FILENAME):
            self._inbox_rel_cache = INBOX_FILENAME
            return INBOX_FILENAME
        target_name = INBOX_FILENAME.rsplit("/", 1)[-1].lower()
        try:
            for e in self._list_folder("", recursive=True):
                if e.get(".tag") != "file":
                    continue
                if e.get("name", "").lower() != target_name:
                    continue
                found_rel = self._rel_of(e.get("path_display", e.get("name", "")))
                self._inbox_rel_cache = found_rel
                return found_rel
        except requests.RequestException:
            pass
        # 発見できなければ既定値のまま（新規作成時等のフォールバック）
        self._inbox_rel_cache = INBOX_FILENAME
        return INBOX_FILENAME

    # -- 高水準操作 --------------------------------------------------------
    def load_notes(self):
        """Vault 配下の .md を再帰列挙し、00_Daily_ToDo を除外して読み込む。"""
        notes = []
        for e in self._list_folder("", recursive=True):
            if e.get(".tag") != "file":
                continue
            name = e.get("name", "")
            if not name.lower().endswith(".md"):
                continue
            rel = self._rel_of(e.get("path_display", name))
            if EXCLUDED_DIR_NAME in rel.split("/"):
                continue
            txt = self.read_text(rel)
            if txt is None:
                continue
            notes.append({"rel": rel, "text": txt, "chars": len(txt)})
        return notes

    def cleanup_inbox(self, saved_date_strs):
        rel = self._resolve_rel(INBOX_FILENAME)
        text = self.read_text(rel)
        if text is None:
            return 0
        new_text, removed, removed_blocks = _compute_inbox_cleanup(text, saved_date_strs)
        if removed:
            self.make_backup(rel)  # .bak を Dropbox 上に作成
            self.write_text(rel, new_text)
            self._record_done_tasks(rel, removed_blocks)
        return removed

    def copy_fx_scenario(self, target_date):
        """平日のみ FX テンプレートを日付付きで複製（土日/既存/テンプレ無しはスキップ）。"""
        if target_date.weekday() >= 5:
            return False, "weekend"
        template_rel = f"{FX_SCENARIO_DIR}/{FX_SCENARIO_TEMPLATE}"
        tpl = self.read_text(template_rel)
        if tpl is None:
            return False, "no_template"
        dest_rel = f"{FX_SCENARIO_DIR}/{target_date.strftime('%y%m%d')}{FX_SCENARIO_TEMPLATE}"
        if self.exists(dest_rel):
            return False, "already_exists"
        try:
            self.write_text(dest_rel, tpl)
            return True, dest_rel
        except requests.RequestException as e:
            return False, f"error: {e}"

    def archive(self, target_date):
        """毎月1日のみ発動。前月ファイルを月次フォルダへ、1月1日はさらに年次フォルダへ移動。"""
        if target_date.day != 1:
            return {}
        if target_date.month == 1:
            prev_year, prev_month = target_date.year - 1, 12
        else:
            prev_year, prev_month = target_date.year, target_date.month - 1
        prev_month_str = f"{prev_year}-{prev_month:02d}"
        fx_prefix = f"{str(prev_year)[-2:]}{prev_month:02d}"

        def _move_matching_files(dir_rel, dest_sub, predicate):
            moved = 0
            for e in self._list_folder(dir_rel, recursive=False):
                if e.get(".tag") != "file":
                    continue
                name = e.get("name", "")
                if predicate(name):
                    if self._move(f"{dir_rel}/{name}", f"{dir_rel}/{dest_sub}/{name}"):
                        moved += 1
            return moved

        monthly_daily = _move_matching_files(
            EXCLUDED_DIR_NAME, prev_month_str,
            lambda nm: nm.startswith(f"{prev_month_str}-") and nm.endswith(".md"),
        )
        monthly_fx = _move_matching_files(
            FX_SCENARIO_DIR, prev_month_str,
            lambda nm: nm.startswith(fx_prefix) and nm.endswith(".md"),
        )
        result = {
            "monthly_folder": prev_month_str,
            "monthly_daily": monthly_daily,
            "monthly_fx": monthly_fx,
            "yearly_folder": None,
            "yearly_daily": None,
            "yearly_fx": None,
        }
        if target_date.month == 1:
            prev_year_str = str(prev_year)
            month_dirs = [f"{prev_year}-{m:02d}" for m in range(1, 13)]

            def _move_dirs(dir_rel):
                moved = 0
                for name in month_dirs:
                    if self.exists(f"{dir_rel}/{name}"):
                        if self._move(f"{dir_rel}/{name}", f"{dir_rel}/{prev_year_str}/{name}"):
                            moved += 1
                return moved

            result.update({
                "yearly_folder": prev_year_str,
                "yearly_daily": _move_dirs(EXCLUDED_DIR_NAME),
                "yearly_fx": _move_dirs(FX_SCENARIO_DIR),
            })
        return result

    def write_daily(self, date_str: str, content: str) -> str:
        rel = f"{EXCLUDED_DIR_NAME}/{date_str}.md"
        self.write_text(rel, content)
        return rel

    def read_cache(self) -> dict:
        txt = self.read_text(DROPBOX_CACHE_REL)
        if not txt:
            return {}
        try:
            data = json.loads(txt)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def write_cache(self, cache: dict) -> None:
        try:
            self.write_text(DROPBOX_CACHE_REL, json.dumps(cache, ensure_ascii=False, indent=2))
        except Exception:  # noqa: BLE001
            pass

    # -- 診断（UI のデバッグパネルから呼ばれる）--------------------------------
    def diagnose(self) -> dict:
        """Dropbox 接続・パス設定の問題を切り分けるための診断情報を集める。

        「.md が1件も見つからない」原因が (a) 認証情報の間違い、(b) パス設定ミス、
        (c) Full Dropbox / App folder のアクセス権スコープの取り違え、
        (d) 本当にファイルが無い、のどれかを UI 上で判別できるようにする。
        """
        result = {
            "configured_base": self.base or "(空文字 = ルート)",
            "auth_ok": False,
            "auth_error": None,
            "account_email": None,
            "root_listing": None,     # トークン自身のルート（"" ）を list_folder した結果
            "root_error": None,
            "base_listing": None,     # 設定された base パスを list_folder した結果
            "base_error": None,
            "inbox_resolved_path": None,   # 実際に読み書きに使われている Inbox の相対パス
            "inbox_self_healed": False,    # True なら直接パスが失敗し自動発見で代替した
        }
        # 1) 認証確認（アクセストークンが取得できるか）
        try:
            self._bearer()
            result["auth_ok"] = True
        except Exception as e:  # noqa: BLE001
            result["auth_error"] = str(e)
            return result  # 認証できなければ以降は無意味

        # 2) アカウント情報（どの Dropbox アカウントに繋がっているか）
        try:
            r = requests.post(
                f"{self._RPC}/users/get_current_account",
                headers=self._headers(), timeout=15,
            )
            if r.status_code == 200:
                result["account_email"] = r.json().get("email")
        except requests.RequestException as e:
            result["account_error"] = str(e)

        # 3) トークン自身のルート（"" ）を列挙。
        #    App Folder スコープのアプリなら、ここに Vault の中身が直接見える。
        #    Full Dropbox スコープなら、ここには "Apps" 等の最上位フォルダが見える。
        try:
            r = requests.post(
                f"{self._RPC}/files/list_folder",
                headers=self._headers(),
                json={"path": "", "recursive": False, "limit": 50},
                timeout=30,
            )
            if r.status_code == 200:
                entries = r.json().get("entries", [])
                result["root_listing"] = [
                    f"{'📁' if e.get('.tag') == 'folder' else '📄'} {e.get('name')}"
                    for e in entries
                ]
            else:
                result["root_error"] = r.json() if r.content else {"status": r.status_code}
        except requests.RequestException as e:
            result["root_error"] = str(e)

        # 4) 設定された DROPBOX_VAULT_PATH（base）を列挙。
        #    409（path/not_found）の場合は「パス自体が存在しない」と「パスは存在するが
        #    中身が空」を区別できるよう、base_listing は None のままにして base_error に詳細を残す。
        try:
            entries = self._list_folder("", recursive=False)
            if self.last_list_error is not None:
                result["base_error"] = self.last_list_error
            else:
                result["base_listing"] = [
                    f"{'📁' if e.get('.tag') == 'folder' else '📄'} {e.get('name')}"
                    for e in entries
                ]
        except requests.RequestException as e:
            result["base_error"] = str(e)

        # 5) Inbox の実体パス解決状況（直接パス失敗時の自己修復が発動したか）
        resolved = self._resolve_rel(INBOX_FILENAME)
        result["inbox_resolved_path"] = resolved
        result["inbox_self_healed"] = resolved != INBOX_FILENAME
        return result


# プロセス内でストレージインスタンスを再利用（トークン再取得の抑制）。
# NameError ガードにより Streamlit の再実行をまたいで保持される。
try:
    _STORAGE_MEMO
except NameError:
    _STORAGE_MEMO = {}


def get_vault_storage(vault_dir: str = None):
    """環境に Dropbox 認証があれば DropboxStorage、無ければ LocalStorage を返す（メモ化）。"""
    key = "dropbox" if _dropbox_configured() else f"local:{vault_dir or DEFAULT_VAULT_PATH}"
    inst = _STORAGE_MEMO.get(key)
    if inst is None:
        inst = DropboxStorage() if key == "dropbox" else LocalStorage(vault_dir or DEFAULT_VAULT_PATH)
        _STORAGE_MEMO[key] = inst
    return inst


# ---------------------------------------------------------------------------
# ヘッドレス生成パイプライン（朝5時自動生成／GitHub Actions／手動テスト共用・UI 非依存）
# ---------------------------------------------------------------------------
_HEADLESS_LOCK = threading.Lock()


def run_headless_generation(vault_dir: str = None, target_date: date = None) -> dict:
    """UI を介さずに、対象日の ToDo を【キャッシュ強制バイパス】でゼロから生成し、
    保存・Inbox お掃除・FX複製・アーカイブ・GitHub Push まで一気通貫で行う。

    引数を省略可能にし、GitHub Actions から `run_headless_generation()` を無引数で
    呼べるようにした。環境に Dropbox 認証があれば読み書きは自動的に Dropbox API 経由へ
    切り替わり（PC OFF でもクラウド完結）、無ければ従来どおりローカルディスクを使う。

    日付判定の正典 `_date_tag_verdict`（事前フィルタ＋最終防壁）はそのまま経由するため、
    翌日以降のタスクは1件も紛れ込まない。戻り値は UI 表示・ログ用の実行サマリ dict。
    """
    if target_date is None:
        target_date = today_jst()
    date_str = target_date.strftime("%Y-%m-%d")
    summary = {
        "date": date_str,
        "storage": None,
        "ok": False,
        "saved_path": None,
        "inbox_removed": 0,
        "fx": None,
        "archive": None,
        "github": None,
        "final_dropped": 0,
        "error": None,
        "ran_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _HEADLESS_LOCK:
        try:
            storage = get_vault_storage(vault_dir)
            summary["storage"] = storage.kind
            # 1) キャッシュ強制バイパス: 毎回ストレージから最新の Vault を直接パース
            notes = storage.load_notes()
            # 2) 対象日の ToDo を生成（事前フィルタ→Gemini→救済→最終防壁は既存の堅牢ロジック）
            results = generate_todos_for_dates([target_date], notes)
            body = results.get(date_str, "")
            # 3) todo_cache.json を最新状態でクリーンに上書き（ローカル実行時の高速表示用）
            try:
                cache = load_cache()
                cache[date_str] = body
                save_cache(cache)
            except Exception:  # noqa: BLE001
                pass
            # 4) 書き込み直前に最終防壁を再適用（冪等）し、日次ファイルを保存
            inbox_text = storage.read_text(storage._resolve_rel(INBOX_FILENAME)) or ""
            banner_value = _parse_banner_text(inbox_text)
            body_clean, final_dropped = enforce_today_only_output(body, target_date)
            summary["final_dropped"] = final_dropped
            content = build_daily_file_content(body_clean, banner_value)
            daily_rel = f"{EXCLUDED_DIR_NAME}/{date_str}.md"
            storage.write_text(daily_rel, content)
            summary["saved_path"] = f"[{storage.kind}] {daily_rel}"
            # 5) Inbox お掃除（範囲保持ロジックは cleanup 側で担保・書き戻しも同ストレージへ）
            try:
                summary["inbox_removed"] = storage.cleanup_inbox([date_str])
            except Exception:  # noqa: BLE001
                pass
            # 6) FX シナリオ複製（平日のみ・土日は自動スキップ）
            summary["fx"] = storage.copy_fx_scenario(target_date)
            # 7) 月次/年次アーカイブ（毎月1日のみ発動）
            summary["archive"] = storage.archive(target_date)
            # 8) GitHub 自動 Push（動的コミットメッセージ）
            github_path = f"{EXCLUDED_DIR_NAME}/{date_str}.md"
            summary["github"] = push_to_github(
                file_path=github_path,
                content=content,
                commit_message=f"auto: Generate Daily ToDo & Backup for {date_str}",
            )
            summary["ok"] = True
        except Exception as e:  # noqa: BLE001
            summary["error"] = str(e)
    return summary


# ---------------------------------------------------------------------------
# 毎朝5時（JST）自動生成スケジューラ（プロセス内シングルトン・常駐デーモンスレッド）
# ---------------------------------------------------------------------------
class _DailyAutogenScheduler:
    """毎日 JST 05:00 に run_headless_generation を発火する常駐スケジューラ。"""

    def __init__(self, vault_dir: str, hour: int = 5):
        self.vault_dir = vault_dir
        self.hour = hour
        self._thread = None
        self._stop = threading.Event()
        self.started_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        self.last_summary = None
        self.last_error = None

    def _seconds_until_target(self) -> float:
        now = datetime.now(JST)
        tgt = now.replace(hour=self.hour, minute=0, second=0, microsecond=0)
        if now >= tgt:
            tgt += timedelta(days=1)
        return (tgt - now).total_seconds()

    def next_run_str(self) -> str:
        nxt = datetime.now(JST) + timedelta(seconds=self._seconds_until_target())
        return nxt.strftime("%Y-%m-%d %H:%M")

    def _loop(self):
        while not self._stop.is_set():
            # 目標時刻まで小刻みに待機（停止要求・時刻ずれに追随）
            end = time.monotonic() + self._seconds_until_target()
            while not self._stop.is_set():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(30.0, remaining))
            if self._stop.is_set():
                break
            # 5:00 到達 → 当日分をキャッシュバイパスで自動生成＆Push
            try:
                self.last_summary = run_headless_generation(self.vault_dir, today_jst())
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
            # 同一 05:00 の二重発火を防ぐため少し進めてから次周回
            time.sleep(120)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        # Secrets をスレッドから確実に読めるよう環境変数へ退避（get_secret がフォールバック）
        for k in ("GEMINI_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_BRANCH"):
            v = get_secret(k)
            if v:
                os.environ.setdefault(k, str(v))
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="daily-5am-autogen"
        )
        self._thread.start()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


@st.cache_resource(show_spinner=False)
def get_daily_autogen_scheduler(vault_dir: str, hour: int = 5):
    """プロセス内で唯一のスケジューラを生成・起動して返す（rerun/セッション間で共有）。

    `st.cache_resource` により (vault_dir, hour) ごとに1度だけ生成されるため、
    Streamlit の再実行のたびにスレッドが増殖することはない。
    """
    sched = _DailyAutogenScheduler(vault_dir, hour=hour)
    sched.start()
    return sched


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Obsidian ToDo 生成", page_icon="✅", layout="centered")
st.title("✅ Obsidian ToDo リスト生成")
st.caption("選んだ日付に該当するタスクを、Obsidianノートから動的に抽出します。")

# APIキーのチェック
api_ok = True
if not GEMINI_API_KEY:
    st.error(
        "`GEMINI_API_KEY` が設定されていません。"
        "`.streamlit/secrets.toml` または Streamlit Cloud の Secrets を確認してください。"
    )
    api_ok = False

# --- サイドバー: Vault 選択 ---
with st.sidebar:
    st.header("📚 ノート設定")
    vault_labels = list(VAULT_PRESETS.keys()) + [CUSTOM_PATH_LABEL]
    vault_choice = st.selectbox("🗂️ Vault（保管庫）", vault_labels, index=0)
    if vault_choice == CUSTOM_PATH_LABEL:
        vault_dir = (
            st.text_input(
                "フォルダパスを直接入力",
                placeholder=r"C:\Users\easyg\Documents\任意のVault",
            )
            .strip()
            .strip('"')
        )
    else:
        vault_dir = VAULT_PRESETS[vault_choice]

    vault_ok = False
    if _dropbox_configured():
        # クラウドモード: Vault は Dropbox 上。ローカルフォルダの有無は問わない。
        vault_ok = True
        _dbx_path = get_secret("DROPBOX_VAULT_PATH") or "(ルート)"
        st.success(f"☁️ クラウドモード（Dropbox）: `{_dbx_path}`")
    elif not vault_dir:
        st.info("フォルダパスを入力してください。")
    elif not Path(vault_dir).is_dir():
        st.error("指定されたフォルダが見つかりません")
    else:
        vault_ok = True
        st.caption(f"📁 `{vault_dir}`")

    st.caption(f"モデル: `{MODEL_ID}`")
    if st.button("🔄 ノートを再読み込み"):
        load_raw_notes.clear()
        scan_md_files.clear()
        st.rerun()

    # --- 新規 ToDo を Inbox に直接追加（## 単発 へ日付昇順で挿入） ---
    st.divider()
    st.markdown("#### 📥 Inboxに新規ToDoを追加")
    st.caption(
        "繰り返しは種ノートに記法で追記します： "
        "曜日 `[w:: Mon,Wed,Fri]` / 毎日 `[w:: Everyday]`（英語3文字）、"
        "月次 `[m:: 13]` または `[m:: 1,15]`、年次 `[y:: 6,11]`（月,日）。"
    )

    # カテゴリーは Inbox から動的抽出してプルダウン化（末尾に「(新規入力)」）
    NEW_CATEGORY_LABEL = "(新規入力)"
    existing_categories = extract_categories(vault_dir) if vault_ok else []
    category_options = existing_categories + [NEW_CATEGORY_LABEL]

    new_title = st.text_input("タスクタイトル", placeholder="例: メルマガ原稿を書く", key="new_title")
    new_use_date = st.checkbox("日付（date::）を付ける", value=True, key="new_use_date")
    new_date = st.date_input("日付（開始日）", value=today_jst(), key="new_task_date")

    # 期間（範囲）指定（日付ありの場合のみ有効）
    new_use_range = st.checkbox("📅 期間（範囲）を指定する", value=False, key="new_use_range", disabled=not new_use_date)
    new_end_date = None
    if new_use_date and new_use_range:
        new_end_date = st.date_input("終了日", value=new_date, key="new_task_end_date")
        if new_end_date < new_date:
            st.warning("終了日が開始日より前です。")

    # 時間指定（日付ありの場合のみ有効）
    TIME_SINGLE = "特定の時間（例: 10:00）"
    TIME_RANGE = "時間帯を指定（例: 10:00-12:00）"
    new_use_time = st.checkbox("時間を指定する", value=False, key="new_use_time", disabled=not new_use_date)
    time_mode = None
    t_start = t_end = None
    if new_use_date and new_use_time:
        time_mode = st.radio("時間の指定方法", [TIME_SINGLE, TIME_RANGE], key="new_time_mode")
        if time_mode == TIME_SINGLE:
            t_start = st.time_input(
                "時間", value=dt_time(10, 0), step=timedelta(minutes=1), key="new_time_start"
            )
        else:
            tc1, tc2 = st.columns(2)
            t_start = tc1.time_input(
                "開始", value=dt_time(10, 0), step=timedelta(minutes=1), key="new_time_start"
            )
            t_end = tc2.time_input(
                "終了", value=dt_time(12, 0), step=timedelta(minutes=1), key="new_time_end"
            )

    cat_choice = st.selectbox("カテゴリー（category::）", category_options, key="new_cat_choice")
    if cat_choice == NEW_CATEGORY_LABEL:
        # 「(新規入力)」選択時のみテキスト欄を出す親切設計
        new_category = st.text_input("新しいカテゴリー名", placeholder="例: ブログwork", key="new_cat_text")
    else:
        new_category = cat_choice

    new_priority = st.selectbox("優先度（priority::）", ["高", "中", "低", "未設定"], index=1, key="new_priority")

    if st.button("📥 Inboxに追加"):
        if not vault_ok:
            st.error("Vault が未設定のため追加できません。")
        elif not new_title.strip():
            st.error("タスクタイトルを入力してください。")
        else:
            # 日付（任意で範囲）＋（任意で）時間を [date:: ...] 用の文字列に組み立てる
            date_value = None
            if new_use_date:
                date_value = new_date.strftime("%Y-%m-%d")
                # 期間指定 ON: 開始日-終了日 のレンジ形式（ハイフン繋ぎ）
                if new_use_range and new_end_date is not None:
                    date_value += f"-{new_end_date.strftime('%Y-%m-%d')}"
                # 時間指定 ON: 末尾に時間（範囲日付の後ろにも結合可能）
                if new_use_time and t_start is not None:
                    if time_mode == TIME_RANGE and t_end is not None:
                        date_value += f" {t_start.strftime('%H:%M')}-{t_end.strftime('%H:%M')}"
                    else:
                        date_value += f" {t_start.strftime('%H:%M')}"
            ok = append_task_to_inbox(
                vault_dir,
                new_title,
                date_value,
                new_category,
                new_priority,
            )
            if ok:
                st.success("Inbox の「## 単発」に日付順で追加しました！")
                st.rerun()
            else:
                st.error("Inbox への追記に失敗しました。")

ready = api_ok and vault_ok
all_notes = load_raw_notes(vault_dir) if ready else []

# 毎朝5時（JST）自動生成スケジューラを常駐起動（プロセス内シングルトン）。
# vault が有効なときのみ。cache_resource により再実行してもスレッドは増えない。
# GitHub Actions 等のヘッドレス import（HEADLESS_JOB=1）ではスレッドを起動しない。
_HEADLESS_ENV = os.environ.get("HEADLESS_JOB") == "1"
autogen_scheduler = (
    get_daily_autogen_scheduler(vault_dir)
    if (vault_ok and not _HEADLESS_ENV)
    else None
)

# --- サイドバー: デバッグ情報（パスの見える化） ---
with st.sidebar:
    with st.expander("🛠️ デバッグ情報", expanded=False):
        _cloud = _dropbox_configured()
        # 実際にスキャンしている Vault のパスを表示（クラウド時は Dropbox パス）
        if _cloud:
            abs_vault = f"[Dropbox] {get_secret('DROPBOX_VAULT_PATH') or '(ルート)'}"
            exists_ok = True
        else:
            abs_vault = os.path.abspath(vault_dir) if vault_dir else "(未指定)"
            exists_ok = bool(vault_dir and Path(vault_dir).is_dir())
        st.markdown("**スキャン対象 Vault**")
        st.code(abs_vault, language="text")
        st.caption(
            f"存在: {'✅ あり' if exists_ok else '❌ なし'}　/　"
            f"モード: {'☁️ クラウド(Dropbox)' if _cloud else '💻 ローカル'}　/　"
            f"除外フォルダ名: `{EXCLUDED_DIR_NAME}`"
        )

        # フォルダ内で検出できた .md ファイルの一覧（除外前/後）
        if _cloud:
            _rels = [n["rel"] for n in load_raw_notes(vault_dir)]
            scan = {"all": _rels, "excluded": [], "loaded": _rels}
        else:
            scan = scan_md_files(vault_dir) if vault_dir else {"all": [], "excluded": [], "loaded": []}
        st.markdown(
            f"**検出した .md ファイル**　"
            f"（全 {len(scan['all'])} 件 / 読込 {len(scan['loaded'])} 件 / "
            f"除外 {len(scan['excluded'])} 件）"
        )
        if scan["loaded"]:
            st.caption("📖 読み込み対象（種ノート）:")
            for rel in scan["loaded"]:
                st.write(f"- ✅ `{rel}`")
        if scan["excluded"]:
            st.caption(f"🚫 {EXCLUDED_DIR_NAME} により除外:")
            for rel in scan["excluded"]:
                st.write(f"- ⛔ `{rel}`")
        if not scan["all"]:
            st.warning("このフォルダ内に .md ファイルが1件も見つかりません。")
            if _cloud:
                _storage = get_vault_storage(vault_dir)
                _err = getattr(_storage, "last_list_error", None)
                if _err:
                    st.error(
                        f"⚠️ Dropbox が `{_err.get('path')}` を **path/not_found** "
                        "として返しました（＝そのパスがDropbox上に存在しません）。"
                    )
                    st.code(json.dumps(_err.get("detail"), ensure_ascii=False, indent=2), language="json")

        # --- Dropbox 接続診断（クラウドモードのみ）---
        if _cloud:
            st.divider()
            if st.button("🔍 Dropbox 接続診断を実行", key="dbx_diag_btn"):
                with st.spinner("Dropbox に接続して診断中…"):
                    diag = get_vault_storage(vault_dir).diagnose()
                st.markdown("**認証**")
                if diag["auth_ok"]:
                    st.success(f"✅ 認証成功" + (f"（アカウント: `{diag['account_email']}`）" if diag.get("account_email") else ""))
                else:
                    st.error(f"❌ 認証失敗: {diag['auth_error']}")
                st.markdown(f"**設定された `DROPBOX_VAULT_PATH`**: `{diag['configured_base']}`")

                st.markdown("**① トークン自身のルート（`\"\"`）の中身**")
                st.caption(
                    "ここに Vault の中身（ToDo_Inbox 等）が直接見えるなら、その Dropbox アプリは "
                    "**App folder** スコープです → `DROPBOX_VAULT_PATH` は空にするか、"
                    "ここに表示されたフォルダ名だけを指定してください（`/Apps/...` は不要）。"
                )
                if diag["root_listing"] is not None:
                    if diag["root_listing"]:
                        for item in diag["root_listing"]:
                            st.write(f"- {item}")
                    else:
                        st.caption("(空)")
                else:
                    st.error(f"取得失敗: {diag['root_error']}")

                st.markdown("**② 設定された `DROPBOX_VAULT_PATH` の中身**")
                if diag["base_listing"] is not None:
                    if diag["base_listing"]:
                        for item in diag["base_listing"]:
                            st.write(f"- {item}")
                        st.success("✅ このパスは存在し、中身が読めています。")
                    else:
                        st.warning("パスは存在しますが、中身が空です。")
                elif diag["base_error"]:
                    st.error(
                        "❌ このパスは Dropbox 上に見つかりません（path/not_found）。"
                        "①の一覧と見比べて `DROPBOX_VAULT_PATH` を修正してください。"
                    )
                    st.code(json.dumps(diag["base_error"], ensure_ascii=False, indent=2, default=str), language="json")

                st.markdown("**③ Inbox（カテゴリ/バナー/新規追加が参照するファイル）**")
                if diag["inbox_self_healed"]:
                    st.warning(
                        f"⚠️ `DROPBOX_VAULT_PATH` + `{INBOX_FILENAME}` の直接パスでは"
                        "見つからなかったため、Vault全体を検索して自動的に "
                        f"`{diag['inbox_resolved_path']}` を発見し、代わりに使用しています。"
                        "\n\n動作はしますが、`DROPBOX_VAULT_PATH` の設定が実際のVaultルートと"
                        "ズレている可能性が高いです。①②の一覧を見比べて正しい値に修正することを推奨します。"
                    )
                else:
                    st.success(f"✅ `{diag['inbox_resolved_path']}` を直接パスで読み書きできています。")

if ready and len(all_notes) == 0:
    st.warning("`.md` ファイルが1件も見つかりませんでした。フォルダパスを確認してください。")
    if _dropbox_configured():
        st.info(
            "☁️ クラウド(Dropbox)モードで動作中です。サイドバーの「🛠️ デバッグ情報」内にある"
            "「🔍 Dropbox 接続診断を実行」ボタンで原因を特定できます。"
            "よくある原因: `DROPBOX_VAULT_PATH` が実際のフォルダと一致していない、"
            "または Dropbox アプリの権限スコープ（Full Dropbox / App folder）の取り違え。"
        )

# ---------------------------------------------------------------------------
# 日付・期間指定 UI
# ---------------------------------------------------------------------------
TODAY = today_jst()  # 日本時間（JST）での本日
WEEK_SPAN = 7  # デフォルト表示の日数（今日を含む7日間）

MODE_SINGLE = "1日だけ選択"
MODE_RANGE = "期間指定（開始日〜終了日）"


def reset_state():
    """設定をリセットし、デフォルト（当日から一週間分）の表示に戻す。

    キャッシュ（todo_cache.json）は消さず、表示ウィンドウと日付ウィジェットのみ初期化する。
    """
    for key in (
        "display_window",
        "date_mode_radio",
        "single_date",
        "range_start",
        "range_end",
    ):
        st.session_state.pop(key, None)
    st.rerun()


st.markdown("### 📅 抽出する日付・期間")
date_mode = st.radio(
    "日付の指定方法",
    [MODE_SINGLE, MODE_RANGE],
    index=0,
    horizontal=True,
    key="date_mode_radio",
)

if date_mode == MODE_SINGLE:
    sel = st.date_input("日付", value=TODAY, key="single_date")
    target_dates = [sel]
else:
    c1, c2 = st.columns(2)
    start_date = c1.date_input("開始日", value=TODAY, key="range_start")
    end_date = c2.date_input("終了日", value=TODAY + timedelta(days=WEEK_SPAN - 1), key="range_end")
    if start_date > end_date:
        st.warning("開始日が終了日より後になっています。")
        target_dates = []
    else:
        span = (end_date - start_date).days
        target_dates = [start_date + timedelta(days=i) for i in range(span + 1)]

col_gen, col_force, col_reset = st.columns(3)
generate = col_gen.button(
    "🗒️ 未生成分を生成",
    type="primary",
    disabled=not (ready and all_notes),
    help="表示中の日付のうち、キャッシュに無い日だけを Gemini で生成してマージします。",
)
force_regen = col_force.button(
    "🔄 AIで全日程を強制再生成",
    disabled=not (ready and all_notes),
    help="表示中の全日程を Gemini で再生成し、キャッシュを上書きします（トークンを消費）。",
)
if col_reset.button("🔄 設定をリセット"):
    reset_state()

# ---------------------------------------------------------------------------
# 毎朝5時 自動生成スケジューラのステータス & 手動テスト実行
# ---------------------------------------------------------------------------
with st.expander("🌅 毎朝5時（JST）自動生成スケジューラ", expanded=False):
    if autogen_scheduler is None:
        st.caption("Vault が未設定のためスケジューラは停止中です。")
    else:
        alive = "🟢 稼働中" if autogen_scheduler.is_alive() else "🔴 停止"
        st.caption(
            f"状態: {alive}　｜　次回自動生成: **{autogen_scheduler.next_run_str()}**（JST 05:00）"
            f"　｜　起動: {autogen_scheduler.started_at}"
        )
        st.caption(
            "⚠️ この自動生成は本アプリ（Streamlit）が起動している間だけ動作します。"
            "PC スリープ／アプリ終了中の 5:00 は発火しません。"
        )
        last = autogen_scheduler.last_summary
        if last:
            gh = last.get("github")
            gh_txt = "―"
            if gh:
                gh_txt = "✅成功" if gh[0] else f"⚠️{gh[1]}"
            st.success(
                f"直近の自動生成: {last['ran_at']} / 対象 {last['date']} / "
                f"保存 {'✅' if last['ok'] else '❌'} / GitHub {gh_txt}"
            )
        if autogen_scheduler.last_error:
            st.warning(f"直近のスケジューラエラー: {autogen_scheduler.last_error}")

        if st.button(
            "🌅 今すぐ『朝5時ジョブ』を実行（テスト）",
            disabled=not (ready and vault_ok),
            help="本日分をキャッシュバイパスで即時生成し、ローカル保存・FX複製・アーカイブ・GitHub Push まで実行します。",
        ):
            with st.spinner("本日分をキャッシュバイパスで生成中…"):
                res = run_headless_generation(vault_dir, today_jst())
            autogen_scheduler.last_summary = res
            if res["ok"]:
                st.success(f"✅ 本日分（{res['date']}）を生成しました: `{res['saved_path']}`")
                if res["inbox_removed"]:
                    st.caption(f"🧹 Inbox から {res['inbox_removed']} 件の日付付きタスクを削除。")
                if res.get("final_dropped"):
                    st.caption(f"🛡️ 最終防壁が今日以外の日付タスク {res['final_dropped']} 件を排除。")
                fx = res.get("fx")
                if fx and fx[0]:
                    st.caption(f"📈 FXシナリオ複製: `{Path(fx[1]).name}`")
                gh = res.get("github")
                if gh and gh[0]:
                    st.caption("☁️ GitHub への自動 Push 完了。")
                elif gh and "未設定" in str(gh[1]):
                    st.caption("☁️ GitHub Secrets 未設定のため Push をスキップ。")
                elif gh:
                    st.warning(f"⚠️ GitHub Push 失敗: {gh[1]}")
            else:
                st.error(f"生成に失敗しました: {res['error']}")
            st.rerun()

# ---------------------------------------------------------------------------
# 表示対象ウィンドウの決定
#   - 生成/再生成ボタン押下 → 選択中の日付/期間
#   - それ以外（起動時・再実行時）→ 直近の表示ウィンドウ、無ければ当日から一週間分
# ---------------------------------------------------------------------------
if (generate or force_regen) and target_dates:
    window = sorted(target_dates)
elif "display_window" in st.session_state:
    window = [date.fromisoformat(s) for s in st.session_state["display_window"]]
else:
    window = [TODAY + timedelta(days=i) for i in range(WEEK_SPAN)]
st.session_state["display_window"] = [d.strftime("%Y-%m-%d") for d in window]

# ---------------------------------------------------------------------------
# キャッシュ運用（トークン節約＆高速化）
#   - 通常表示はキャッシュからのみ読み込み（Gemini を叩かない）
#   - 「未生成分を生成」: キャッシュに無い日のみ生成してマージ
#   - 「強制再生成」: 全日程を生成してキャッシュ上書き
# ---------------------------------------------------------------------------
cache = load_cache()

if (generate or force_regen) and ready and all_notes:
    if force_regen:
        to_gen = list(window)
    else:  # generate: キャッシュに無い日付のみ
        to_gen = [d for d in window if d.strftime("%Y-%m-%d") not in cache]

    if not to_gen:
        st.info("表示中の日付はすべてキャッシュ済みです（生成をスキップしました）。")
    else:
        # 【キャッシュ強制バイパス】強制再生成時は st.cache_data のノートキャッシュを破棄し、
        # ディスクから最新の ToDo_Inbox.md を直接パースして最新状態を確実に反映する。
        if force_regen:
            load_raw_notes.clear()
            scan_md_files.clear()
            gen_notes = read_notes_fresh(vault_dir)  # Dropbox 認証時はクラウドから直接
            st.caption("♻️ 強制再生成: キャッシュを無視し最新の Inbox を直接読み込みました。")
        else:
            gen_notes = all_notes
        with st.spinner(f"{len(to_gen)}日分のToDoを Gemini で生成中…"):
            new_results = generate_todos_for_dates(to_gen, gen_notes)
        cache.update(new_results)
        save_cache(cache)

# ---------------------------------------------------------------------------
# 生成済み ToDo の表示（時系列・日付ごとセクション）
# ---------------------------------------------------------------------------
ordered_dates = [d.strftime("%Y-%m-%d") for d in window]
banner_value = read_inbox_banner(vault_dir) if vault_ok else DEFAULT_BANNER

st.divider()
st.info(
    "⚡ 表示はローカルキャッシュ（`todo_cache.json`）から高速読込しています。"
    "内容を最新化するには「🗒️ 未生成分を生成」または「🔄 AIで全日程を強制再生成」を押してください。"
    f"　🖼️ バナー: `{banner_value}`"
)

shown_any = False
for date_str in ordered_dates:
    try:
        d = date.fromisoformat(date_str)
        weekday_jp = WEEKDAY_JP[d.weekday()]
    except ValueError:
        weekday_jp = "?"

    st.markdown(f"### {date_str} ({weekday_jp})")
    body = cache.get(date_str)
    if body is None:
        st.caption("（未生成。「未生成分を生成」または「強制再生成」で抽出できます）")
        continue
    shown_any = True
    # st.code はクリックでコピーできるコンポーネント
    st.code(body, language="markdown")

    # この日のToDoだけを 00_Daily_ToDo にローカル生成するボタン（曜日ごと個別）
    if st.button("📅 この日のToDoファイルを生成", key=f"gen_file_{date_str}"):
        if not vault_ok:
            st.error("Vault が未設定のため保存できません。")
        else:
            # 【最終防壁】書き込み直前に今日以外の日付タスクを物理排除（冪等）
            body_clean, final_dropped = enforce_today_only_output(body, d)
            content = build_daily_file_content(body_clean, banner_value)
            saved_path = write_daily_file_local(vault_dir, date_str, content)
            # 「## 単発」かつ [date::] を持つ該当タスクを Inbox から自動削除
            removed = cleanup_inbox_singles(vault_dir, [date_str])
            st.success(f"✅ `{saved_path}` を生成しました（banner: {banner_value}）。")
            if final_dropped:
                st.caption(
                    f"🛡️ 書き込み直前フィルターが今日以外の日付タスク {final_dropped} 件を"
                    "強制排除しました。"
                )
            if removed:
                st.caption(f"🧹 Inbox の「## 単発」から {removed} 件の日付付きタスクを削除しました。")
            # FX シナリオテンプレートの複製（平日のみ）
            fx_ok, fx_msg = copy_fx_scenario_template(vault_dir, d)
            if fx_ok:
                st.caption(f"📈 FXシナリオファイルを生成しました: `{Path(fx_msg).name}`")
            elif fx_msg == "no_template":
                st.warning(
                    f"⚠️ FXシナリオテンプレート（`{FX_SCENARIO_TEMPLATE}`）が"
                    f"`{FX_SCENARIO_DIR}/` 内に見つかりません。手動で配置してください。"
                )
            # 月次 / 年次アーカイブ（月初め 1 日のみ実行）
            arc = archive_past_files(vault_dir, d)
            if arc:
                mf  = arc["monthly_folder"]
                mfd = arc["monthly_daily"]
                mfx = arc["monthly_fx"]
                if mfd or mfx:
                    st.caption(
                        f"📁 前月分のファイルを `{mf}` フォルダへアーカイブしました"
                        f"（Daily: {mfd}件、FX: {mfx}件）。"
                    )
                if arc.get("yearly_folder"):
                    yf  = arc["yearly_folder"]
                    yfd = arc["yearly_daily"]
                    yfx = arc["yearly_fx"]
                    st.caption(
                        f"🗂 {yf}年分の月次フォルダを `{yf}/` へ年次アーカイブしました"
                        f"（Daily: {yfd}フォルダ、FX: {yfx}フォルダ）。"
                    )
            # GitHub への自動 Push（ファイル生成直後にクラウドバックアップ）
            github_path = f"{EXCLUDED_DIR_NAME}/{date_str}.md"
            gh_ok, gh_msg = push_to_github(
                file_path=github_path,
                content=content,
                commit_message=f"auto: Generate Daily ToDo & Backup for {date_str}",
            )
            if gh_ok:
                st.caption(f"☁️ GitHubへの自動バックアップも完了しました（`{github_path}`）。")
            elif "未設定" in gh_msg:
                # Secrets 未設定はオプション扱い: エラーではなく情報として表示
                st.caption("☁️ GitHub Secrets 未設定のため自動バックアップをスキップしました。")
            else:
                st.warning(f"⚠️ GitHubへの自動バックアップに失敗しました: {gh_msg}")
            st.rerun()

if not shown_any:
    st.warning("表示対象の日付にキャッシュがありません。上のボタンで生成してください。")

# ---------------------------------------------------------------------------
# GitHub 一括保存（表示中・キャッシュ済みの全日分）
# ---------------------------------------------------------------------------
savable_dates = [ds for ds in ordered_dates if ds in cache]
if savable_dates and st.button("📂 Obsidianへ保存（GitHubへ一括Push）"):
    successes, failures = [], []
    with st.spinner(f"{len(savable_dates)}件のファイルを GitHub へコミット中…"):
        for date_str in savable_dates:
            github_path = f"{EXCLUDED_DIR_NAME}/{date_str}.md"
            # 【最終防壁】Push 直前に今日以外の日付タスクを物理排除（冪等）
            try:
                _d_save = date.fromisoformat(date_str)
                _body_save, _ = enforce_today_only_output(cache[date_str], _d_save)
            except ValueError:
                _body_save = cache[date_str]
            content = build_daily_file_content(_body_save, banner_value)
            ok, info = push_to_github(
                file_path=github_path,
                content=content,
                commit_message=f"Add daily ToDo: {date_str}.md",
            )
            if ok:
                successes.append(github_path)
            else:
                failures.append(f"`{github_path}`: {info}")

    # GitHub へ正常に移せたら、対応する「## 単発」日付付きタスクを Inbox から削除
    if successes and vault_ok:
        moved_dates = [p.split("/")[-1].replace(".md", "") for p in successes]
        removed = cleanup_inbox_singles(vault_dir, moved_dates)
        if removed:
            st.caption(f"🧹 Inbox の「## 単発」から {removed} 件の日付付きタスクを削除しました。")

        # FX シナリオテンプレートの複製（平日のみ・ローカルコピー）
        fx_copied, fx_no_template = [], False
        for ds in moved_dates:
            try:
                d_obj = date.fromisoformat(ds)
            except ValueError:
                continue
            fx_ok, fx_msg = copy_fx_scenario_template(vault_dir, d_obj)
            if fx_ok:
                fx_copied.append(Path(fx_msg).name)
            elif fx_msg == "no_template":
                fx_no_template = True
        if fx_copied:
            st.caption("📈 FXシナリオファイルを生成しました:\n" + "\n".join(f"- `{n}`" for n in fx_copied))
        if fx_no_template:
            st.warning(
                f"⚠️ FXシナリオテンプレート（`{FX_SCENARIO_TEMPLATE}`）が"
                f"`{FX_SCENARIO_DIR}/` 内に見つかりません。手動で配置してください。"
            )

    if successes and not failures:
        st.success(
            "GitHubへの自動Pushに成功しました！Obsidianを開いて同期を待ってください。"
        )
        st.caption("保存したファイル:\n" + "\n".join(f"- `{p}`" for p in successes))
    elif successes and failures:
        st.warning(f"{len(successes)}件は成功しましたが、{len(failures)}件失敗しました。")
        st.caption("成功:\n" + "\n".join(f"- `{p}`" for p in successes))
        st.error("失敗:\n" + "\n".join(f"- {f}" for f in failures))
    else:
        st.error("保存に失敗しました:\n" + "\n".join(f"- {f}" for f in failures))
