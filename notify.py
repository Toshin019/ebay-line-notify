#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# notify.py
# eBay Browse API で新着出品を検索し、前回チェック時になかった「新着だけ」を
# LINE Messaging API の push message で自分に通知する本体スクリプト。
# 認証情報はコードに書かず、環境変数から読み込みます。

import base64
import json
import os
import sys
import time
import requests

# --- 認証情報は環境変数から取得（コードには絶対に書かない） ---
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

# --- 監視したいキーワード（選手名など）。ここを自由に足し引きする ---
KEYWORDS = [
    "Shohei Ohtani",
    "Tomoyuki Sugano",
    "Munetaka Murakami",
]

MARKETPLACE = "EBAY_US"        # 米国eBayを対象
RESULTS_PER_KEYWORD = 20       # 各キーワードで確認する新着件数
INCLUDE_AUCTION = True         # True: オークションも含める / False: Buy It Nowのみ
MAX_NOTIFY_PER_RUN = 10        # 1回の実行で送る通知の上限（大量通知の防止）

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
PUSH_URL = "https://api.line.me/v2/bot/message/push"

# 通知済みの出品IDを覚えておくファイル（このスクリプトと同じフォルダに作られる）
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


def search_newly_listed(token, keyword, limit):
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
    }
    params = {
        "q": keyword,
        "sort": "newlyListed",
        "limit": limit,
    }
    if INCLUDE_AUCTION:
        params["filter"] = "buyingOptions:{AUCTION|FIXED_PRICE}"
    r = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
    if r.status_code != 200:
        print(f"  [{keyword}] 検索に失敗 (status {r.status_code}): {r.text}")
        return []
    return r.json().get("itemSummaries", [])


def line_push(text):
    headers = {
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}],
    }
    r = requests.post(PUSH_URL, headers=headers, json=payload, timeout=30)
    if r.status_code != 200:
        print(f"  LINE送信に失敗 (status {r.status_code}): {r.text}")
        return False
    return True


def load_seen():
    """通知済みIDの集合を読み込む。ファイルが無ければ None（初回）を返す。"""
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


def format_message(keyword, item):
    title = item.get("title", "(no title)")
    price = item.get("price", {}) or {}
    price_str = f"{price.get('value', '?')} {price.get('currency', '')}".strip()
    url = item.get("itemWebUrl", "")
    options = ",".join(item.get("buyingOptions", []))
    return (
        f"🆕 eBay新着 [{keyword}]\n"
        f"{title}\n"
        f"💰 {price_str}  ({options})\n"
        f"{url}"
    )


def main():
    check_env()
    seen = load_seen()
    first_run = seen is None
    if first_run:
        seen = set()

    token = get_ebay_token()

    # 全キーワードを検索して、現在の出品を集める
    current = []  # (keyword, item, item_id) のリスト
    for kw in KEYWORDS:
        items = search_newly_listed(token, kw, RESULTS_PER_KEYWORD)
        for it in items:
            item_id = it.get("itemId")
            if item_id:
                current.append((kw, it, item_id))

    if first_run:
        # 初回は「今ある出品」を基準として記録するだけ。通知はしない（大量通知の防止）
        for _, _, item_id in current:
            seen.add(item_id)
        save_seen(seen)
        print(f"初回実行のため、現在の{len(seen)}件を基準として記録しました。")
        print("次回以降、新しく出た分だけを通知します。")
        return

    # 前回になかった新着だけを抽出
    new_items = [(kw, it, iid) for (kw, it, iid) in current if iid not in seen]

    if not new_items:
        print("新着はありませんでした。")
        return

    print(f"新着 {len(new_items)} 件を検出。通知します。")
    sent = 0
    for kw, it, item_id in new_items:
        if sent >= MAX_NOTIFY_PER_RUN:
            print(f"（今回は上限{MAX_NOTIFY_PER_RUN}件まで通知。残りは次回に回します）")
            break
        if line_push(format_message(kw, it)):
            seen.add(item_id)  # 送れたものだけ「通知済み」に記録
            sent += 1
            time.sleep(0.5)    # 連続送信をやさしく間引く

    save_seen(seen)
    print(f"{sent} 件を通知しました。")


if __name__ == "__main__":
    main()
