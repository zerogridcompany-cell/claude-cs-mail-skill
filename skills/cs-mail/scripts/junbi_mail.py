#!/usr/bin/env /usr/bin/python3
"""注文から3日後の「準備してます」メールを出す道具。

なんで要るか（2026-09-17）:
  客に届くのは「注文確認」と「発送完了」の2通だけ。その間の3〜10日が真っ暗で、
  「何の連絡もない」という問い合わせが量産されとる（実測: その日の問い合わせ4件中2件がこれ）。

やること:
  1. 注文の一覧を読む（Shopifyから）
  2. 「注文から3日以上たった・まだ発送してない・払い済み・まだ送ってない」客を選ぶ
  3. 文面を組み立てる（正典 = templates/junbi.md。環境変数 CS_JUNBI_TEMPLATE で差し替え可）
  4. CSアドレスから1通ずつ送る（csmail.py send）→ 送信済みトレイで検算
  5. 台帳に書く（二度送らんため）

使い方:
  junbi-mail.py --orders-json <file>            # 下見だけ（既定。1通も送らん）
  junbi-mail.py --orders-json <file> --send     # 実際に送る
  junbi-mail.py --orders-json <file> --limit 5  # 1回の上限を変える（既定15）
  junbi-mail.py --daicho                        # 台帳の中身を出す

注文JSONの形（1件1行でも、配列でも、Shopifyの生レスポンスでもええ）:
  {"name":"#1001","createdAt":"2026-01-10T11:34:11Z","email":"x@example.com",
   "displayFinancialStatus":"PAID","displayFulfillmentStatus":"UNFULFILLED",
   "customer":{"firstName":"花子","lastName":"山田"}}

🔴 鉄則
  - 差出人はCSアドレス（ブラウザにCSアドレスだけでログインしておく）
  - 台帳にある注文には二度と送らん
  - 発送済み・キャンセル済み・未払いには送らん
  - 1回の上限を超えたら止まる（暴発よけ）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from namae_check import yobina          # noqa: E402

HERE = os.environ.get("CS_HOME", os.path.expanduser("~/cs-mail"))
KATA = os.environ.get("CS_JUNBI_TEMPLATE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates", "junbi.md"))
DAICHO = f"{HERE}/送信済み_準備メール.json"
CSMAIL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csmail.py")

# 何日たったら出すか（実測: 発送まで5〜9日なので、3日目なら必ずまだ発送前）
HIZUKE = 3
# 文面に書く発送の目安。🔴 実測が伸びたらここを直す（根拠は 文面/準備中.md の表）
MEDO = "7〜10日"
# 1回に出す上限。暴発よけ
JOUGEN = 15
# 1通ごとに空ける秒数（Gmailに連投と見なされんため）
AIDA = 20


# ---------- 台帳 ----------

def daicho_yomu():
    if not os.path.exists(DAICHO):
        return {}
    with open(DAICHO, encoding="utf-8") as f:
        return json.load(f)


def daicho_kaku(d):
    tmp = DAICHO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, DAICHO)


# ---------- 文面 ----------

def kata_yomu():
    """正典の .md から件名と本文を切り出す。```で囲まれた最初の2つを使う。"""
    with open(KATA, encoding="utf-8") as f:
        src = f.read()
    blocks = re.findall(r"```\n(.*?)\n```", src, re.S)
    if len(blocks) < 2:
        sys.exit(f"🔴 型が読めん: {KATA} に ``` で囲んだ件名と本文が要る")
    return blocks[0].strip(), blocks[1].strip()


def namae(o):
    """🔴 名前は必ず注文データから。メールの表示名からは読まん（漢字を読み違える）。
    🔴 客が姓と名を逆に入れとることがある（実測 50件中2件）。
       namae_check で検算して、決めきれんかったら「お客様」に落とす。推測で書かん。"""
    c = o.get("customer") or {}
    n, riyuu = yobina(c.get("firstName"), c.get("lastName"))
    o["_namae_riyuu"] = riyuu
    return n or "お客様"


def kumitateru(o, kenmei_kata, honbun_kata):
    ire = {"{名前}": namae(o), "{番号}": o["name"], "{発送目安}": MEDO}
    kenmei, honbun = kenmei_kata, honbun_kata
    for k, v in ire.items():
        kenmei = kenmei.replace(k, v)
        honbun = honbun.replace(k, v)
    nokori = set(re.findall(r"\{[^}]+\}", kenmei + honbun))
    if nokori:
        sys.exit(f"🔴 埋まってへん差し込みが残っとる: {sorted(nokori)}")
    return kenmei, honbun


# ---------- 選ぶ ----------

def orders_yomu(path):
    """Shopifyの生レスポンスでも、配列でも、1行1件でも読めるようにする。"""
    with open(path, encoding="utf-8") as f:
        src = f.read().strip()
    if not src:
        return []
    try:
        d = json.loads(src)
    except json.JSONDecodeError:
        return [json.loads(l) for l in src.splitlines() if l.strip()]
    while isinstance(d, dict):
        for k in ("data", "orders", "nodes", "edges"):
            if k in d:
                d = d[k]
                break
        else:
            return [d]
    if d and isinstance(d[0], dict) and "node" in d[0]:
        d = [x["node"] for x in d]
    return d


def jogai_yomu():
    """手で「この人には出さんといて」と決めた注文番号。1行1件。
    使いどき: もう個別に返事した客・怒っとる客・事情がある客。
    書き方:  #1001 山田様 もう個別に返事した     ← 先頭の番号だけ見る。後ろはメモ
             // で始まる行は読み飛ばす（#はメモに使えん。番号と間違えるため）"""
    path = f"{HERE}/準備メール_除外.txt"
    if not os.path.exists(path):
        return set()
    out = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("//"):
                continue
            s = s.split()[0]
            out.add(s if s.startswith("#") else "#" + s)
    return out


JOGAI = jogai_yomu()


def erabu(orders, dc, ima):
    """出す相手と、外した相手＋理由を返す。判定はここだけ。手で上書きせん。"""
    dasu, nozoku = [], []
    for o in orders:
        n = o.get("name", "?")
        riyuu = None
        if n in JOGAI:
            riyuu = "除外リストに入っとる"
        elif n in dc:
            riyuu = f"送信済み（{dc[n].get('sent_at', '?')[:10]}）"
        elif o.get("cancelledAt"):
            riyuu = "キャンセル済み"
        elif (o.get("displayFulfillmentStatus") or "").upper() != "UNFULFILLED":
            riyuu = f"発送済み（{o.get('displayFulfillmentStatus')}）"
        elif (o.get("displayFinancialStatus") or "").upper() != "PAID":
            riyuu = f"払い済みやない（{o.get('displayFinancialStatus')}）"
        elif not (o.get("email") or "").strip():
            riyuu = "メールアドレスが無い"
        else:
            c = datetime.fromisoformat(o["createdAt"].replace("Z", "+00:00"))
            hi = (ima - c).days
            o["_keika"] = hi
            if hi < HIZUKE:
                riyuu = f"まだ{hi}日目（{HIZUKE}日たってへん）"
        if riyuu:
            nozoku.append((n, riyuu))
        else:
            dasu.append(o)
    dasu.sort(key=lambda x: x["createdAt"])   # 古い順＝待たせとる人から
    return dasu, nozoku


# ---------- 送る ----------

def okuru(o, kenmei, honbun):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(honbun + "\n")
        path = f.name
    try:
        r = subprocess.run(
            ["/usr/bin/python3", CSMAIL, "send",
             "--to", o["email"], "--subject", kenmei, "--body-file", path],
            capture_output=True, text=True, timeout=300)
        print(r.stdout, end="")
        if r.stderr:
            print(r.stderr, end="", file=sys.stderr)
        # 🔴 押せたか と 確かめられたか は別。押せたのに確認できんかった物も台帳に残す
        #    （残さんと次の日にもう1通飛ぶ）
        oseta = "送信ボタンを押した" in r.stdout
        return oseta, r.returncode == 0
    finally:
        os.unlink(path)


# ---------- 本体 ----------

def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--orders-json")
    p.add_argument("--send", action="store_true")
    p.add_argument("--limit", type=int, default=JOUGEN)
    p.add_argument("--daicho", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    a = p.parse_args()

    if a.help or (not a.orders_json and not a.daicho):
        print(__doc__)
        return 0

    dc = daicho_yomu()

    if a.daicho:
        if not dc:
            print("（台帳はまだ空）")
            return 0
        for n, v in sorted(dc.items()):
            print(f"{n}  {v.get('sent_at', '?')[:16]}  {v.get('to', '')}")
        print(f"\n合計 {len(dc)} 件")
        return 0

    ima = datetime.now(timezone.utc)
    orders = orders_yomu(a.orders_json)
    dasu, nozoku = erabu(orders, dc, ima)
    kenmei_kata, honbun_kata = kata_yomu()

    print(f"読んだ注文: {len(orders)}件 ／ 外した: {len(nozoku)}件 "
          f"／ 出す相手: {len(dasu)}件")
    print(f"しきい: 注文から{HIZUKE}日以上・上限{a.limit}通・発送目安「{MEDO}」")
    print("=" * 60)

    if not dasu:
        print("出す相手なし。終わり。")
        return 0

    for o in dasu:
        n = namae(o)
        r = o.get("_namae_riyuu", "")
        shirushi = "" if r == "ok" else f"  ←名前: {r}"
        print(f"  {o['name']}  {o['_keika']}日目  {n:<10}  {o['email']}{shirushi}")
    if len(dasu) > a.limit:
        print(f"\n🔴 上限{a.limit}通を超えとる（{len(dasu)}件）。"
              f"古い順に{a.limit}件だけ出して、残りは次の回にまわす。")
        dasu = dasu[:a.limit]

    # 1通目の実物を必ず見せる（人が目で読む用）
    k1, h1 = kumitateru(dasu[0], kenmei_kata, honbun_kata)
    print("\n" + "=" * 60)
    print(f"【1通目の実物】To: {dasu[0]['email']}")
    print(f"件名: {k1}")
    print("-" * 60)
    print(h1)
    print("=" * 60)

    if not a.send:
        print(f"\n（下見だけ。1通も送ってへん。出すなら --send を足す）")
        return 0

    ok, ng = [], []
    for i, o in enumerate(dasu, 1):
        kenmei, honbun = kumitateru(o, kenmei_kata, honbun_kata)
        print(f"\n--- {i}/{len(dasu)}  {o['name']}  {o['email']} ---")
        oseta, kakunin = okuru(o, kenmei, honbun)
        if oseta:
            dc[o["name"]] = {"sent_at": datetime.now().isoformat(timespec="seconds"),
                             "to": o["email"], "keika": o["_keika"],
                             "kakunin": kakunin}
            daicho_kaku(dc)          # 🔴 1通ごとに書く（途中で落ちても二重送信せん）
        if oseta and kakunin:
            ok.append(o["name"])
        else:
            ng.append(o["name"] + ("(押せたが未確認)" if oseta else "(押せんかった)"))
        if i < len(dasu):
            time.sleep(AIDA)

    print("\n" + "=" * 60)
    print(f"✅ 送れた {len(ok)}件: {' '.join(ok) if ok else 'なし'}")
    if ng:
        print(f"🔴 送れんかった {len(ng)}件: {' '.join(ng)}　←目で確かめること")
    return 1 if ng else 0


if __name__ == "__main__":
    sys.exit(main())
