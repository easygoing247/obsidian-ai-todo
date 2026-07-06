import os
import re
import shutil

# 対象のファイルパス
file_path = r"C:\Users\easyg\Desktop\フォルダ\ObsidianAiTodo\obsidian-ai-todo\ToDo_Inbox.md"
backup_path = file_path + ".bak"

def convert_tag(match):
    inner_text = match.group(1).strip()

    # 1. 「毎日」の置換
    if inner_text == "毎日":
        return "[w:: 毎日]"

    # 2. 「毎週〇・〇曜」の置換
    if inner_text.startswith("毎週") and inner_text.endswith("曜"):
        # 「毎週」と「曜」を取り除く
        days = inner_text[2:-1]
        # 中黒「・」をカンマ「,」に置換
        days = days.replace("・", ",")
        return f"[w:: {days}]"

    # 3. 「毎月〇日」の置換
    if inner_text.startswith("毎月") and inner_text.endswith("日"):
        # 「毎月」と「日」を取り除く
        dates = inner_text[2:-1]
        # 万が一中黒が使われていたらカンマに置換
        dates = dates.replace("・", ",")
        return f"[m:: {dates}]"

    # どれにも該当しない場合は安全のためそのまま返す
    return match.group(0)

def main():
    if not os.path.exists(file_path):
        print(f"❌ エラー: ファイルが見つかりません\nパス: {file_path}")
        return

    # 安全のためバックアップ（.bak）を作成
    shutil.copy2(file_path, backup_path)
    print(f"📦 安全のためバックアップを作成しました: {backup_path}")

    # ファイルの読み込み
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 旧構文 [frequency:: ...] を見つけて置換
    pattern = r"\[frequency::\s*(.*?)\]"
    new_content, count = re.subn(pattern, convert_tag, content)

    # ファイルへの書き込み
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"✨ 置換が完了しました！")
    print(f"📝 修正件数: {count} 件のタスクを新構文へ自動アップデートしました。")

if __name__ == "__main__":
    main()
