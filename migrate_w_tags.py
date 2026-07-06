import os
import re
import shutil

# 対象ファイル
file_path = r"C:\Users\easyg\Desktop\フォルダ\ObsidianAiTodo\obsidian-ai-todo\ToDo_Inbox.md"
backup_path = file_path + ".bak"

# 置換マップ（「毎日」を先に処理する。順序が重要: 毎日 は 日 を含むため）
REPLACEMENTS = [
    ("毎日", "Everyday"),
    ("月", "Mon"),
    ("火", "Tue"),
    ("水", "Wed"),
    ("木", "Thu"),
    ("金", "Fri"),
    ("土", "Sat"),
    ("日", "Sun"),
]

# [w:: ...] タグの中身だけを対象にする
W_TAG_RE = re.compile(r"\[w::\s*(.*?)\]")


def convert_inner(match):
    inner = match.group(1)
    for jp, en in REPLACEMENTS:
        inner = inner.replace(jp, en)
    return f"[w:: {inner.strip()}]"


def main():
    if not os.path.exists(file_path):
        print(f"X エラー: ファイルが見つかりません\nパス: {file_path}")
        return

    # 安全のためバックアップを作成
    shutil.copy2(file_path, backup_path)
    print(f"[backup] バックアップを作成しました: {backup_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    new_content, count = W_TAG_RE.subn(convert_inner, content)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print("[done] 置換が完了しました！")
    print(f"[count] {count} 件の [w:: ...] タグを英語表記へ変換しました。")


if __name__ == "__main__":
    main()
