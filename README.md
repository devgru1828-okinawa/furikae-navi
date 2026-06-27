# 振替ナビ X-bot 実装ガイド（無料MVP）

個人がXで公開する前提の、最小構成の手順書です。
構成は **ODPTで検知 → Xに自動投稿 → リンク先のWeb(PWA)で現在地入力・経路案内** の3段。
すべて無料の範囲で始められます。

---

## 全体像

```
[ODPT API] --(5分ごとに取得)--> [bot(Python)] --(運転見合わせを検知)--> [Xに自動投稿]
                                                                          │
                                                              投稿内のリンク
                                                                          ▼
                                                       [PWA] 現在地入力 → 代替ルート表示
```

- **検知**：Yahoo!ではなく ODPT（公共交通オープンデータセンター）。正規・無料。
- **投稿**：X無料枠（1日50投稿・書き込みのみ）の範囲。リンクと免責表記つき。
- **現在地入力／経路案内**：Xの中ではなく、投稿に貼ったPWAで行う（読み取り課金を回避）。

---

## 用意するもの（すべて無料）

| 項目 | 用途 | 取得先 | 費用 |
|---|---|---|---|
| ODPT アカウント | 運行情報の取得 | developer.odpt.org で登録 → consumerKey 発行 | 無料 |
| X 開発者アカウント | 自動投稿 | developer.x.com で登録 → APIキー4種 | 無料枠 |
| GitHub アカウント | コード置き場＋無料定期実行＋PWA配信 | github.com | 無料 |

---

## ファイル構成

```
furikae-navi/
├── odpt_x_bot.py          # 検知＋投稿の本体
├── requirements.txt       # 依存ライブラリ
├── .env.example           # 環境変数の見本（ローカル確認用）
├── state.json             # 重複投稿防止の記録（自動生成）
├── .github/workflows/
│   └── bot.yml            # 5分ごとの無料定期実行
└── docs/                  # PWA（プロトタイプHTMLを配置）
    └── index.html         # furikae_navi_prototype.html をリネーム
```

---

## 手順

### 1. ODPT のトークンを取得
1. https://developer.odpt.org/ で無料登録。
2. ログイン後、アクセストークン（consumerKey）を発行。
3. 監視したい事業者のデータ提供状況を確認（JR東日本・東京メトロ・都営など）。
   - 一部データは「チャレンジ用」と「本番用」で取得元が分かれる場合があるため、利用区分を確認。

### 2. ローカルで動作確認（投稿せずに中身だけ見る）
```bash
pip install -r requirements.txt
cp .env.example .env        # .env を編集して ODPT_TOKEN を設定、DRY_RUN=true のまま
export $(cat .env | xargs)  # 環境変数を読み込み（mac/Linux）
python odpt_x_bot.py
```
`DRY_RUN=true` なら、投稿せず「こんな文面を投稿する」という内容だけ表示されます。
文面・対象路線・キーワードはここで調整します。

### 3. X の API キーを取得
1. https://developer.x.com/ で開発者登録。
2. アプリを作成し、**Read and Write** 権限を付与。
3. API Key / API Secret / Access Token / Access Token Secret の4つを控える。

> 注意（2026年時点）：Xは新規に無料枠が縮小。無料枠は「月1,500・1日50投稿まで・書き込みのみ」。
> 本MVPは投稿のみで読み取りを使わないため無料枠で動きますが、超過分や読み取りは従量課金です。
> 1日上限はスクリプト側でも45件に制限済み（`DAILY_LIMIT`）。

### 4. PWA（現在地入力ページ）を公開
1. これまで作った `furikae_navi_prototype.html` を `docs/index.html` にリネームして配置。
2. GitHubリポジトリの Settings > Pages で、ソースを `main` ブランチの `/docs` に設定。
3. 数分で `https://ユーザー名.github.io/furikae-navi/` で公開される。
4. このURLを `.env` / Secrets の `PWA_BASE_URL` に設定（投稿リンク先になる）。

### 5. GitHub Actions で無料の自動運用
1. `bot.yml` を `.github/workflows/bot.yml` に配置。
2. リポジトリ Settings > Secrets and variables > Actions に次を登録：
   `ODPT_TOKEN` `X_API_KEY` `X_API_SECRET` `X_ACCESS_TOKEN` `X_ACCESS_SECRET` `PWA_BASE_URL`
3. これで5分ごとに自動実行。運転見合わせを検知すると自動投稿します。
   - `state.json` をコミットして同じ内容の重複投稿を防止します。

---

## 投稿文のイメージ

```
🚨 JR中央線快速 運転見合わせ
人身事故のため、東京〜三鷹間で運転を見合わせています…
▶ 振替・代替ルートを確認: https://ユーザー名.github.io/furikae-navi/
※所要時間は一般的な目安です。振替輸送は各鉄道会社の公式サイトをご確認ください。
公式運行情報: https://traininfo.jreast.co.jp/train_info/
#運行情報 #振替ナビ
```

---

## 守るべきルール・注意点

- **Xの自動化ポリシー**：botであることをプロフィールで明示。同一文面の連投・スパム的挙動を避ける（本スクリプトは重複防止と1日上限で対応済み）。
- **免責表記は必ず維持**：振替実施の可否は断定せず、公式サイトへ誘導。所要時間は「一般的な目安」と明記。
- **ODPTの利用規約**：取得データの表示・出典表記など提供条件を確認。
- **誤情報リスク**：検知元の遅延・欠測がありうるため、断定的表現を避ける文面のままにする。
- **コスト監視**：万一読み取り機能を足す場合は従量課金が発生。無料で続けるなら「投稿のみ」を維持。

---

## 段階的な拡張（あとから）

1. 路線名マップ（`RAILWAY_JA`）を充実させ、対象事業者を拡大。
2. PWAに現在地→最寄り駅→代替候補の表示ロジックを実装（経路探索APIは無料/オープンデータから）。
3. 投稿に路線別のディープリンク（`?line=...`）を付け、PWA側で該当路線を初期表示。
4. 反応が増えたら、読み取りAPI（有料）でリプライ対応や、ネイティブアプリ化を検討。
```
