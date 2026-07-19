#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# notify.py  (v6: Buy It Now限定＋キーワードごとの価格上限)
# eBay Browse API で新着出品を検索し、前回チェック時になかった「新着だけ」を
# LINE Messaging API の push message で自分に通知する本体スクリプト。
# 認証情報はコードに書かず、環境変数から読み込みます。

import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
import requests

# --- 認証情報は環境変数から取得（コードには絶対に書かない） ---
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

# --- 監視したいキーワード（選手名など）。ここを自由に足し引きする ---
# q         : 検索キーワード
# max_price : この価格（USD）以下だけ通知する。上限なしにしたいときは None
KEYWORDS = [
    {"q": "(leaf,topps) samuel jackson (auto,autograph)", "max_price": 500},
    {"q": "(leaf,topps) johnny depp (auto,autograph) -1987",    "max_price": 700},
    {"q": "2026 prizm shinji ono gold (auto,autograph)",  "max_price": 300},
    {"q": "topps munetaka murakami (auto,autograph)",    "max_price": 2000},
]

MARKETPLACE = "EBAY_US"        # 米国eBayを対象
RESULTS_PER_KEYWORD = 20       # 各キーワードで確認する新着件数
INCLUDE_AUCTION = False        # True: オークションも含める / False: Buy It Nowのみ
MAX_NOTIFY_PER_RUN = 10        # 1回の実行で通知する上限（LINEの無料枠を節約）
# 出品されてからこの時間より古いものは通知しない（古い出品の掘り起こしを防ぐ）
MAX_AGE_HOURS = 24

# LINEは1リクエストにつき最大5つの吹き出しをまとめて送れる。
# 通数は「リクエスト数」でカウントされるため、5件ずつまとめると無料枠を節約できる。
BUBBLES_PER_REQUEST = 5

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
PUSH_URL = "https://api.line.me/v2/bot/message/push"

# 通知済みの出品IDを覚えておくファイル
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "seen_items.json")


def check_env():
    missing = [name for name, val in [
        ("EBAY_CLIENT_ID", EBAY_CLIENT_ID),
        ("EBAY_CLIENT_SECRET", EBAY_CLIENT_SECRET),
        ("LINE_CHANNEL_ACCESS_TOKEN", LINE_TOKEN),
        ("LINE_USER_ID", LINE_USER_ID),
    ] if not val]
    if missing:
        sys.exit("環境変数が未設定です: " + ", ".join(missing))


def get_ebay_token():
    creds = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode("utf-8")
    b64 = base64.b64encode(creds).decode("ascii")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {b64}",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    r = requests.post(OAUTH_URL, headers=headers, data=data, timeout=30)
    if r.status_code != 200:
        sys.exit(f"eBayトークン取得に失敗 (status {r.status_code}): {r.text}")
    return r.json()["access_token"]


def search_newly_listed(token, keyword, max_price, limit):
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
    }
    params = {
        "q": keyword,
        "sort": "newlyListed",
        "limit": limit,
    }

    # eBay側で絞り込んでもらう（余計なデータを受け取らずに済む）
    filters = []
    if INCLUDE_AUCTION:
        filters.append("buyingOptions:{AUCTION|FIXED_PRICE}")
    else:
        filters.append("buyingOptions:{FIXED_PRICE}")
    if max_price:
        filters.append(f"price:[..{max_price}]")
        filters.append("priceCurrency:USD")
    params["filter"] = ",".join(filters)

    r = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
    if r.status_code != 200:
        print(f"  [{keyword}] 検索に失敗 (status {r.status_code}): {r.text}")
        return []
    return r.json().get("itemSummaries", [])


def get_image_url(item):
    """出品の画像URLを取り出す。少し大きめのサイズに差し替える。"""
    url = (item.get("image") or {}).get("imageUrl")
    if not url:
        thumbs = item.get("thumbnailImages") or []
        if thumbs:
            url = thumbs[0].get("imageUrl")
    if not url:
        return None
    if not url.startswith("https://"):
        return None  # LINEはhttpsの画像しか表示できない
    # eBayの画像URLは末尾が s-l225.jpg のようになっている。大きめの500に差し替える
    return re.sub(r"/s-l\d+\.(jpg|png|webp)", r"/s-l500.\1", url)


def is_fresh(item):
    """出品日時が MAX_AGE_HOURS 以内かどうかを判定する。
    日時が取れない場合は安全側に倒して「新しい」とみなす。"""
    created = item.get("itemCreationDate")
    if not created:
        return True
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)


def build_item_url(item):
    """出品ページのURLを、余計な追跡パラメータを外した形に整える。
    さらに openExternalBrowser=1 を付け、LINE内ブラウザではなく
    端末の標準ブラウザ（iPhoneならSafari）で開くようにする。
    Safariで開くとeBayアプリがインストールされていればアプリに引き継がれる。"""
    raw = item.get("itemWebUrl", "")
    m = re.search(r"/itm/(\d+)", raw)
    if m:
        clean = f"https://www.ebay.com/itm/{m.group(1)}"
    elif raw.startswith("https://"):
        clean = raw.split("?")[0]   # クエリ部分を落とす
    else:
        clean = "https://www.ebay.com/"
    sep = "&" if "?" in clean else "?"
    return f"{clean}{sep}openExternalBrowser=1"


def build_bubble(keyword, item):
    """1件分のカード（バブル）を組み立てる。"""
    title = item.get("title", "(no title)")
    price = item.get("price", {}) or {}
    price_str = f"{price.get('value', '?')} {price.get('currency', '')}".strip()
    url = build_item_url(item)
    options = ", ".join(item.get("buyingOptions", [])) or "-"
    image_url = get_image_url(item)

    body_contents = [
        {"type": "text", "text": f"🆕 {keyword}"[:60],
         "size": "xxs", "color": "#888888", "wrap": True},
        {"type": "text", "text": title[:150],
         "weight": "bold", "size": "sm", "wrap": True, "maxLines": 4,
         "margin": "sm"},
        {"type": "text", "text": price_str,
         "size": "xl", "weight": "bold", "color": "#1DB446", "margin": "md"},
        {"type": "text", "text": options,
         "size": "xxs", "color": "#AAAAAA", "margin": "xs"},
    ]

    bubble = {
        "type": "bubble",
        "size": "kilo",
        "body": {"type": "box", "layout": "vertical", "contents": body_contents},
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [{
                "type": "button",
                "style": "primary",
                "height": "sm",
                "color": "#0064D2",
                "action": {"type": "uri", "label": "eBayで見る", "uri": url},
            }],
        },
    }

    # 画像があればカード上部に大きく表示（タップでも出品ページへ）
    if image_url:
        bubble["hero"] = {
            "type": "image",
            "url": image_url,
            "size": "full",
            "aspectRatio": "1:1",
            "aspectMode": "fit",
            "backgroundColor": "#FFFFFF",
            "action": {"type": "uri", "uri": url},
        }

    return bubble


def line_push_messages(messages):
    """組み立てたメッセージをまとめて送信する。"""
    headers = {
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"to": LINE_USER_ID, "messages": messages}
    r = requests.post(PUSH_URL, headers=headers, json=payload, timeout=30)
    if r.status_code != 200:
        print(f"  LINE送信に失敗 (status {r.status_code}): {r.text}")
        return False
    return True


def load_seen():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=0)


def main():
    check_env()
    seen = load_seen()
    first_run = seen is None
    if first_run:
        seen = set()

    token = get_ebay_token()

    current = []
    for entry in KEYWORDS:
        # 文字列だけで書かれていた場合にも対応する
        if isinstance(entry, str):
            entry = {"q": entry, "max_price": None}
        kw = entry["q"]
        max_price = entry.get("max_price")
        items = search_newly_listed(token, kw, max_price, RESULTS_PER_KEYWORD)
        for it in items:
            item_id = it.get("itemId")
            if item_id:
                current.append((kw, it, item_id))

    if first_run:
        for _, _, item_id in current:
            seen.add(item_id)
        save_seen(seen)
        print(f"初回実行のため、現在の{len(seen)}件を基準として記録しました。")
        print("次回以降、新しく出た分だけを通知します。")
        return

    unseen = [(kw, it, iid) for (kw, it, iid) in current if iid not in seen]

    # 出品が古いものは「通知せずに既読扱い」にして、以後は対象から外す
    new_items = []
    stale = 0
    for kw, it, iid in unseen:
        if is_fresh(it):
            new_items.append((kw, it, iid))
        else:
            seen.add(iid)
            stale += 1
    if stale:
        print(f"{stale} 件は出品から{MAX_AGE_HOURS}時間以上経過しているため通知しません。")

    if not new_items:
        save_seen(seen)
        print("新着はありませんでした。")
        return

    # 上限まで絞る（残りは次回の実行に持ち越し）
    targets = new_items[:MAX_NOTIFY_PER_RUN]
    if len(new_items) > MAX_NOTIFY_PER_RUN:
        print(f"新着 {len(new_items)} 件を検出（今回は{MAX_NOTIFY_PER_RUN}件まで通知）。")
    else:
        print(f"新着 {len(new_items)} 件を検出。通知します。")

    sent_ids = []
    # 1件ごとに独立したカード（縦に並ぶ）を作り、5件ずつ1リクエストで送る
    for i in range(0, len(targets), BUBBLES_PER_REQUEST):
        chunk = targets[i:i + BUBBLES_PER_REQUEST]
        messages = []
        for kw, it, _ in chunk:
            title = it.get("title", "新着")
            messages.append({
                "type": "flex",
                "altText": f"eBay新着: {title}"[:390],
                "contents": build_bubble(kw, it),
            })

        if line_push_messages(messages):
            sent_ids.extend([iid for _, _, iid in chunk])

    for iid in sent_ids:
        seen.add(iid)
    save_seen(seen)
    print(f"{len(sent_ids)} 件を通知しました。")


if __name__ == "__main__":
    main()
