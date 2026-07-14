"""
振替ナビ Bot
ODPT APIで運転見合わせを検知 → Xに投稿する

GitHub Actionsで5分ごとに実行される。
"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

import requests
import tweepy

# ─── ログ設定 ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ─── 環境変数 ─────────────────────────────────────────────
ODPT_TOKEN      = os.environ["ODPT_TOKEN"]
X_API_KEY       = os.environ["X_API_KEY"]
X_API_SECRET    = os.environ["X_API_SECRET"]
X_ACCESS_TOKEN  = os.environ["X_ACCESS_TOKEN"]
X_ACCESS_SECRET = os.environ["X_ACCESS_SECRET"]
PWA_BASE_URL    = os.environ.get("PWA_BASE_URL", "").rstrip("/")
DRY_RUN         = os.environ.get("DRY_RUN", "false").lower() == "true"

# ─── 定数 ─────────────────────────────────────────────────
ODPT_BASE   = "https://api.odpt.org/api/4/odpt:TrainInformation"
STATE_FILE  = "state.json"
MAX_POSTS_PER_DAY = 45       # X無料枠50件に対して安全マージン
MAX_STATE_ENTRIES = 500      # state.jsonの上限エントリ数

# 対象キーワード（運転見合わせのみ）
TRIGGER_KEYWORDS = ["運転見合わせ", "見合わせ", "運転を見合わせ"]

# 路線名マッピング（odpt:Railway → 表示名）
RAILWAY_NAMES: dict[str, str] = {
    "odpt.Railway:JR-East.ChuoRapid":       "JR中央線快速",
    "odpt.Railway:JR-East.ChuoSobuLocal":   "JR中央・総武線各駅停車",
    "odpt.Railway:JR-East.Yamanote":        "JR山手線",
    "odpt.Railway:JR-East.Keihin-Tohoku":   "JR京浜東北線",
    "odpt.Railway:JR-East.Joban":           "JR常磐線",
    "odpt.Railway:JR-East.Sobu":            "JR総武線快速",
    "odpt.Railway:TokyoMetro.Ginza":        "東京メトロ銀座線",
    "odpt.Railway:TokyoMetro.Marunouchi":   "東京メトロ丸ノ内線",
    "odpt.Railway:TokyoMetro.Hibiya":       "東京メトロ日比谷線",
    "odpt.Railway:TokyoMetro.Tozai":        "東京メトロ東西線",
    "odpt.Railway:TokyoMetro.Chiyoda":      "東京メトロ千代田線",
    "odpt.Railway:TokyoMetro.Yurakucho":    "東京メトロ有楽町線",
    "odpt.Railway:TokyoMetro.Hanzomon":     "東京メトロ半蔵門線",
    "odpt.Railway:TokyoMetro.Namboku":      "東京メトロ南北線",
    "odpt.Railway:TokyoMetro.Fukutoshin":   "東京メトロ副都心線",
    "odpt.Railway:Toei.Asakusa":            "都営浅草線",
    "odpt.Railway:Toei.Mita":              "都営三田線",
    "odpt.Railway:Toei.Shinjuku":          "都営新宿線",
    "odpt.Railway:Toei.Oedo":              "都営大江戸線",
    "odpt.Railway:Keio.Keio":              "京王線",
    "odpt.Railway:Odakyu.Odawara":         "小田急小田原線",
    "odpt.Railway:Tokyu.Toyoko":           "東急東横線",
    "odpt.Railway:Tokyu.DenToshi":         "東急田園都市線",
    "odpt.Railway:Seibu.Ikebukuro":        "西武池袋線",
    "odpt.Railway:Seibu.Shinjuku":         "西武新宿線",
    "odpt.Railway:Tobu.Skytree":           "東武スカイツリーライン",
    "odpt.Railway:Tobu.Tojo":             "東武東上線",
}

# 運行会社 → 公式サイトURL
OPERATOR_URLS: dict[str, str] = {
    "odpt.Operator:JR-East":      "https://traininfo.jreast.co.jp/train_info/",
    "odpt.Operator:TokyoMetro":   "https://www.tokyometro.jp/unkou/",
    "odpt.Operator:Toei":         "https://www.kotsu.metro.tokyo.jp/tetsudo/",
    "odpt.Operator:Keio":         "https://www.keio.co.jp/train/transfer/",
    "odpt.Operator:Odakyu":       "https://www.odakyu.jp/train/",
    "odpt.Operator:Tokyu":        "https://www.tokyu.co.jp/railway/train_info/",
    "odpt.Operator:Seibu":        "https://www.seiburailway.jp/railways/operation/",
    "odpt.Operator:Tobu":         "https://www.tobu.co.jp/train/",
}

DISCLAIMER = "※所要時間は目安です。振替輸送の詳細は各鉄道会社の公式サイトをご確認ください。"


# ─── 状態管理 ──────────────────────────────────────────────

def load_state() -> dict:
    """state.jsonを読み込む。なければ初期値を返す。"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning("state.json読み込みエラー、初期化します: %s", e)
    return {"posted": [], "daily_count": 0, "last_date": ""}


def save_state(state: dict) -> None:
    """state.jsonを保存する。エントリ数上限を超えたら古いものを削除。"""
    # 古いエントリを削除
    if len(state["posted"]) > MAX_STATE_ENTRIES:
        state["posted"] = state["posted"][-MAX_STATE_ENTRIES:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_today_jst() -> str:
    """今日の日付（JST）を YYYY-MM-DD で返す。"""
    jst = timezone.utc
    # GitHub Actions はUTC。JST = UTC+9
    from datetime import timedelta
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    return now_jst.strftime("%Y-%m-%d")


def make_event_id(railway_id: str, description: str) -> str:
    """重複検知用のMD5ハッシュを生成する。"""
    raw = f"{railway_id}:{description}"
    return hashlib.md5(raw.encode()).hexdigest()


# ─── ODPT API ─────────────────────────────────────────────

def fetch_disruptions() -> list[dict]:
    """ODPTから全路線の運行情報を取得し、運転見合わせのみ返す。"""
    params = {
        "acl:consumerKey": ODPT_TOKEN,
    }
    try:
        resp = requests.get(ODPT_BASE, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log.error("ODPT API取得エラー: %s", e)
        return []

    disruptions = []
    for item in data:
        desc = item.get("odpt:trainInformationText", {})
        # 多言語対応：ja を優先、なければ en
        text = desc.get("ja") or desc.get("en", "")
        if not text:
            continue
        # トリガーキーワードチェック
        if not any(kw in text for kw in TRIGGER_KEYWORDS):
            continue
        disruptions.append({
            "railway":  item.get("odpt:railway", ""),
            "operator": item.get("odpt:operator", ""),
            "text":     text,
        })

    log.info("ODPT取得: 全%d件中、運転見合わせ%d件", len(data), len(disruptions))
    return disruptions


# ─── ポスト生成 ────────────────────────────────────────────

def build_post(railway_id: str, operator_id: str, text: str) -> str:
    """投稿テキストを生成する。X の280文字制限を考慮。"""
    railway_name = RAILWAY_NAMES.get(railway_id, railway_id.split(":")[-1])
    official_url = OPERATOR_URLS.get(operator_id, "")

    lines = [
        f"【運転見合わせ情報】",
        f"🚃 {railway_name}",
        f"",
        text[:80] + ("…" if len(text) > 80 else ""),
        f"",
        DISCLAIMER,
    ]

    if official_url:
        lines.append(f"🔗 公式: {official_url}")

    if PWA_BASE_URL:
        lines.append(f"📱 振替ナビ: {PWA_BASE_URL}")

    return "\n".join(lines)


# ─── X 投稿 ───────────────────────────────────────────────

def post_to_x(text: str) -> bool:
    """Xに投稿する。成功したらTrue。"""
    if DRY_RUN:
        log.info("[DRY RUN] 投稿をスキップ:\n%s", text)
        return True

    try:
        client = tweepy.Client(
            consumer_key=X_API_KEY,
            consumer_secret=X_API_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_SECRET,
        )
        client.create_tweet(text=text)
        log.info("投稿成功")
        return True
    except tweepy.TweepyException as e:
        log.error("X投稿エラー: %s", e)
        return False


# ─── メイン ───────────────────────────────────────────────

def main() -> None:
    state = load_state()
    today = get_today_jst()

    # 日付が変わったら投稿カウントをリセット
    if state.get("last_date") != today:
        log.info("新しい日付 %s: 投稿カウントをリセット", today)
        state["daily_count"] = 0
        state["last_date"] = today

    # 1日の上限チェック
    if state["daily_count"] >= MAX_POSTS_PER_DAY:
        log.warning("本日の投稿上限(%d)に達しています。スキップ。", MAX_POSTS_PER_DAY)
        return

    disruptions = fetch_disruptions()
    if not disruptions:
        log.info("運転見合わせなし。終了。")
        return

    posted_ids: set[str] = set(state.get("posted", []))
    new_posts = 0

    for d in disruptions:
        if state["daily_count"] + new_posts >= MAX_POSTS_PER_DAY:
            log.warning("上限に達したため残りをスキップ")
            break

        event_id = make_event_id(d["railway"], d["text"])
        if event_id in posted_ids:
            log.info("重複スキップ: %s", d["railway"])
            continue

        post_text = build_post(d["railway"], d["operator"], d["text"])
        log.info("投稿:\n%s", post_text)

        if post_to_x(post_text):
            posted_ids.add(event_id)
            state["posted"].append(event_id)
            new_posts += 1

    state["daily_count"] = state.get("daily_count", 0) + new_posts
    save_state(state)
    log.info("完了: 今回%d件投稿 / 本日累計%d件", new_posts, state["daily_count"])


if __name__ == "__main__":
    main()
