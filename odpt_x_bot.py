#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
振替ナビ X-bot (MVP)
ODPT（公共交通オープンデータセンター）で運転見合わせを検知し、
無料枠の範囲で X に自動投稿する最小構成のスクリプト。

方針:
  - 検知元は Yahoo! ではなく ODPT（正規・無料）。
  - 振替の実施可否は断定せず、所要時間は「一般的な目安」、
    振替輸送は各社公式サイトへ誘導する免責表記を必ず付ける。
  - 同じ内容の重複投稿を避けるため state.json で既投稿を記録
    （X 無料枠：1日50投稿・書き込みのみ を守るため）。

必要な環境変数（.env または GitHub Secrets）:
  ODPT_TOKEN              ODPT の consumerKey
  X_API_KEY              X(API) consumer key
  X_API_SECRET          X(API) consumer secret
  X_ACCESS_TOKEN        X access token
  X_ACCESS_SECRET       X access token secret
  PWA_BASE_URL          現在地入力・経路案内ページ(PWA)のURL
"""

import os
import json
import time
import hashlib
import requests

# tweepy は投稿時のみ必要（DRY_RUN では未使用でも動く）
try:
    import tweepy
except ImportError:
    tweepy = None

ODPT_ENDPOINT = "https://api.odpt.org/api/v4/odpt:TrainInformation"
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# 監視する事業者（必要に応じて追加）。ODPT の Operator ID。
OPERATORS = [
    "odpt.Operator:JR-East",
    "odpt.Operator:TokyoMetro",
    "odpt.Operator:Toei",
]

# 投稿トリガーにするキーワード（事故起因の見合わせに限定したい場合）
TRIGGER_KEYWORDS = ["運転見合わせ", "見合わせ", "運転を見合わせ"]

# 路線IDの簡易日本語名マップ（無い場合はIDから推定表示）
RAILWAY_JA = {
    "odpt.Railway:JR-East.ChuoRapid": "JR中央線快速",
    "odpt.Railway:JR-East.ChuoSobuLocal": "JR中央・総武線各駅停車",
    "odpt.Railway:JR-East.Yamanote": "JR山手線",
    "odpt.Railway:JR-East.KeihinTohokuNegishi": "JR京浜東北・根岸線",
    "odpt.Railway:JR-East.SaikyoKawagoe": "JR埼京・川越線",
    "odpt.Railway:TokyoMetro.Marunouchi": "東京メトロ丸ノ内線",
    "odpt.Railway:TokyoMetro.Tozai": "東京メトロ東西線",
    "odpt.Railway:TokyoMetro.Hibiya": "東京メトロ日比谷線",
    "odpt.Railway:Toei.Shinjuku": "都営新宿線",
    "odpt.Railway:Toei.Oedo": "都営大江戸線",
}

# 各社公式 運行情報ページ（免責リンク用）
OFFICIAL_URL = {
    "odpt.Operator:JR-East": "https://traininfo.jreast.co.jp/train_info/",
    "odpt.Operator:TokyoMetro": "https://www.tokyometro.jp/unkou/",
    "odpt.Operator:Toei": "https://www.kotsu.metro.tokyo.jp/subway/schedule/",
}

DISCLAIMER = "※所要時間は一般的な目安です。振替輸送は各鉄道会社の公式サイトをご確認ください。"

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


def jp(field):
    """ODPTの多言語フィールド({'ja': '...'}) から日本語を取り出す。"""
    if isinstance(field, dict):
        return field.get("ja") or field.get("en") or ""
    return field or ""


def railway_name(railway_id):
    if railway_id in RAILWAY_JA:
        return RAILWAY_JA[railway_id]
    # 例: odpt.Railway:JR-East.ChuoRapid -> ChuoRapid
    tail = railway_id.split(".")[-1] if railway_id else "路線"
    return tail


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_train_info(token):
    """ODPTから運行情報を取得して結合リストで返す。"""
    results = []
    for op in OPERATORS:
        try:
            r = requests.get(
                ODPT_ENDPOINT,
                params={"odpt:operator": op, "acl:consumerKey": token},
                timeout=20,
            )
            r.raise_for_status()
            results.extend(r.json())
        except Exception as e:
            print(f"[WARN] {op} の取得に失敗: {e}")
    return results


def is_disruption(item):
    """運転見合わせ等のトリガーに該当するか判定。"""
    status = jp(item.get("odpt:trainInformationStatus"))
    text = jp(item.get("odpt:trainInformationText"))
    blob = f"{status} {text}"
    return any(k in blob for k in TRIGGER_KEYWORDS)


def build_post(item):
    railway_id = item.get("odpt:railway", "")
    operator = item.get("odpt:operator", "")
    name = railway_name(railway_id)
    text = jp(item.get("odpt:trainInformationText")).strip()
    official = OFFICIAL_URL.get(operator, "")
    pwa = os.getenv("PWA_BASE_URL", "https://example.com")

    # 本文（要点のみ。長すぎる公式文は切り詰める）
    reason = text[:60] + ("…" if len(text) > 60 else "")

    lines = [
        f"🚨 {name} 運転見合わせ",
        reason,
        f"▶ 振替・代替ルートを確認: {pwa}",
        DISCLAIMER,
    ]
    if official:
        lines.append(f"公式運行情報: {official}")
    lines.append("#運行情報 #振替ナビ")

    post = "\n".join([l for l in lines if l])
    # X の上限(280字, 日本語は全角=1)に収める保険
    return post[:270]


def dedup_key(item):
    """路線×内容で一意キー。内容が変われば再投稿対象になる。"""
    raw = item.get("odpt:railway", "") + "|" + jp(item.get("odpt:trainInformationText"))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def get_x_client():
    if tweepy is None:
        raise RuntimeError("tweepy 未インストール。`pip install tweepy` を実行してください。")
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )


def main():
    token = os.environ.get("ODPT_TOKEN")
    if not token:
        raise SystemExit("環境変数 ODPT_TOKEN が未設定です。")

    state = load_state()
    posted_keys = set(state.get("posted", []))
    daily_count = state.get("daily_count", 0)
    DAILY_LIMIT = 45  # 無料枠50/日に対して安全マージン

    items = fetch_train_info(token)
    disruptions = [i for i in items if is_disruption(i)]
    print(f"取得 {len(items)} 件 / 見合わせ {len(disruptions)} 件")

    client = None
    new_posts = 0

    for item in disruptions:
        key = dedup_key(item)
        if key in posted_keys:
            continue  # 同内容は投稿済み
        if daily_count + new_posts >= DAILY_LIMIT:
            print("[INFO] 1日の投稿上限に達したため停止。")
            break

        post = build_post(item)
        if DRY_RUN:
            print("----- DRY_RUN（投稿せず表示）-----")
            print(post)
        else:
            if client is None:
                client = get_x_client()
            try:
                client.create_tweet(text=post)
                print(f"[OK] 投稿: {railway_name(item.get('odpt:railway',''))}")
            except Exception as e:
                print(f"[ERROR] 投稿失敗: {e}")
                continue

        posted_keys.add(key)
        new_posts += 1
        time.sleep(2)  # レート配慮

    state["posted"] = list(posted_keys)[-500:]  # 肥大化防止
    state["daily_count"] = daily_count + new_posts
    save_state(state)
    print(f"完了: 新規投稿 {new_posts} 件")


if __name__ == "__main__":
    main()
