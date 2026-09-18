#!/usr/bin/env /usr/bin/python3
"""CS用のGmail受信箱を、ブラウザの遠隔操作口（CDP）越しに読む・送る道具。

公式のGmail連携で見えんアドレス（共有のCS箱など）を、人と同じ画面で扱う。
Claude専用ブラウザ（seat.sh で起動）に、CSアドレスで1回ログインしておくこと。

使い方:
  csmail.py list                        # 受信箱の新しい順
  csmail.py list "検索クエリ"            # Gmailの検索構文がそのまま使える
  csmail.py list --json                 # 機械で読む形（1行1件のJSON）
  csmail.py open "件名の一部"            # そのスレッドを開いて全文を出す
  csmail.py open "件名の一部" --json

例:
  csmail.py list "in:inbox newer_than:1d"
  csmail.py list "{from:a@x.jp from:b@y.jp}"      # 複数アドレスのOR
  csmail.py open "キャンセルさせて下さい"

前提: scripts/seat.sh start （CDP が生きとること。ポートは環境変数 CS_SEAT_PORT・既定 9336）
🔴 このブラウザにはCSアドレスだけでログインしておく＝送信は必ずCSアドレスから出る。
"""
import json, re, sys, time, urllib.parse, urllib.request

import os
PORT = os.environ.get("CS_SEAT_PORT", "9336")


def _tab():
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT")
    return json.load(urllib.request.urlopen(req, timeout=15))


class Seat:
    def __init__(self):
        import websocket
        self.ws = websocket.create_connection(
            _tab()["webSocketDebuggerUrl"], timeout=60)
        self.n = 0
        self.cmd("Page.enable")
        self.cmd("Runtime.enable")
        # 🔴 窓なしやと画面が 0x0 になって、Gmailが一覧を描かん（0件に見える）
        self.cmd("Emulation.setDeviceMetricsOverride",
                 {"width": 1280, "height": 900, "deviceScaleFactor": 1,
                  "mobile": False})

    def cmd(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method,
                                 "params": params or {}}))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == self.n:
                return r

    def js(self, expr):
        r = self.cmd("Runtime.evaluate", {"expression": expr,
                                          "returnByValue": True,
                                          "awaitPromise": True})
        return r["result"]["result"].get("value")

    def go(self, url, wait=8):
        self.cmd("Page.navigate", {"url": url})
        time.sleep(wait)

    def close(self):
        try:
            self.cmd("Page.close")
        except Exception:
            pass


def _url(query):
    if not query:
        return "https://mail.google.com/mail/u/0/#inbox"
    return ("https://mail.google.com/mail/u/0/#search/"
            + urllib.parse.quote(query, safe=""))


ROWS_JS = r"""
(() => {
  const rows = [...document.querySelectorAll('tr.zA')];
  return JSON.stringify(rows.map(r => ({
    unread : r.classList.contains('zE'),
    from   : (r.querySelector('.yW span[email]')?.getAttribute('email'))
             || (r.querySelector('.yW span')?.innerText || '').trim(),
    name   : (r.querySelector('.yW span')?.innerText || '').trim(),
    subject: (r.querySelector('.y6 span')?.innerText || '').trim(),
    snippet: (r.querySelector('.y2')?.innerText || '').trim().replace(/^\s*-\s*/,''),
    when   : (r.querySelector('.xW span')?.getAttribute('title')
             || r.querySelector('.xW span')?.innerText || '').trim(),
  })));
})()
"""


def list_mail(query, as_json):
    s = Seat()
    try:
        s.go(_url(query), wait=3)
        # 🔴 Gmailが重い日は8秒では描き終わらん（9/15実測: タイトル「Zero Grid Mail」のまま0件に見えた）
        for _ in range(20):
            if s.js("document.querySelectorAll('tr.zA').length") or \
               "No messages matched" in (s.js("document.body.innerText") or "") or \
               "一致するメールはありません" in (s.js("document.body.innerText") or ""):
                break
            time.sleep(3)
        # 🔴 ログアウトやと宣伝ページに飛ばされて「0件」に見える。黙って0件を返さず、はっきり落とす
        href = s.js("location.href") or ""
        if "mail.google.com" not in href:
            print("🔴 CS受信箱にログインできてへん（" + href[:60] + "）。seat.sh で開いたブラウザでログインし直すこと",
                  file=sys.stderr)
            sys.exit(3)
        raw = s.js(ROWS_JS)
        rows = json.loads(raw) if raw else []
    finally:
        s.close()
    if as_json:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return rows
    if not rows:
        print("（0件）")
        return rows
    for r in rows:
        mark = "●" if r["unread"] else " "
        print(f"{mark} {r['when']}  {r['name']}  |  {r['subject']}")
        if r["snippet"]:
            print(f"    {r['snippet'][:160]}")
    print(f"\n合計 {len(rows)} 件")
    return rows


OPEN_JS = """
(() => {
  const t = %s;
  const r = [...document.querySelectorAll('tr.zA')].find(x => x.innerText.includes(t));
  if (!r) return 'NOTFOUND';
  (r.querySelector('.bog') || r).click();
  return 'CLICKED';
})()
"""


def open_thread(needle, query, as_json):
    s = Seat()
    try:
        s.go(_url(query))
        if s.js(OPEN_JS % json.dumps(needle)) == "NOTFOUND":
            print(f"🔴 その行が見つからん: {needle}", file=sys.stderr)
            return 1
        time.sleep(6)
        # 折りたたまれた過去メールを開く
        s.js("document.querySelectorAll('.ajT').forEach(e => e.click())")
        time.sleep(3)
        out = {
            "url": s.js("location.href"),
            "title": s.js("document.title"),
            "text": s.js("document.body.innerText"),
        }
    finally:
        s.close()
    # Gmailの左サイドバーは毎回同じなので落とす
    body = out["text"]
    cut = body.find("Print all")
    if cut > 0:
        body = body[cut + len("Print all"):]
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if as_json:
        print(json.dumps({**out, "text": body}, ensure_ascii=False))
    else:
        print(out["title"])
        print("=" * 60)
        print(body[:8000])
    return 0


SEND_JS = r"""
(() => {
  const btns = [...document.querySelectorAll('div[role="button"]')];
  const b = btns.find(x => (x.getAttribute('data-tooltip') || '').startsWith('Send')
                        || (x.getAttribute('aria-label') || '').startsWith('Send')
                        || x.innerText.trim() === 'Send'
                        || x.innerText.trim() === '送信');
  if (!b) return 'NOBUTTON';
  b.click();
  return 'SENT';
})()
"""


def send_mail(to, subject, body_file, dry):
    """CSアドレスから1通出す。🔴 客宛は必ずここから。個人アドレスからは送らん。"""
    body = open(body_file, encoding="utf-8").read().rstrip() + "\n"
    print("=" * 60)
    print(f"To     : {to}")
    print(f"Subject: {subject}")
    print("-" * 60)
    print(body)
    print("=" * 60)
    if dry:
        print("（--dry なので送ってへん）")
        return 0
    url = ("https://mail.google.com/mail/u/0/?view=cm&fs=1&tf=1"
           + "&to=" + urllib.parse.quote(to)
           + "&su=" + urllib.parse.quote(subject)
           + "&body=" + urllib.parse.quote(body))
    s = Seat()
    try:
        s.go(url, wait=10)
        r = s.js(SEND_JS)
        if r != "SENT":
            print(f"🔴 送信ボタンが押せんかった: {r}", file=sys.stderr)
            return 1
        time.sleep(6)
    finally:
        s.close()
    print("送信ボタンを押した。送信済みトレイで検算する…")
    time.sleep(4)
    rows = list_mail(f"in:sent to:{to}", False)
    # 🔴 先頭20字やと同じ客への前のメール（件名の頭が同じ）で誤って「確認できた」になる（9/15実測）。件名全体で照合
    hit = any(subject.strip() == (x["subject"] or "").strip() for x in rows)
    print("✅ 送信済みトレイで確認できた" if hit
          else "🔴 送信済みトレイに見当たらん。目で確かめること")
    return 0 if hit else 1


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    as_json = "--json" in a
    dry = "--dry" in a
    a = [x for x in a if x not in ("--json", "--dry")]
    cmd = a[0]
    if cmd == "send":
        opt = dict(zip(a[1::2], a[2::2]))
        need = ("--to", "--subject", "--body-file")
        if not all(k in opt for k in need):
            print("🔴 使い方: csmail.py send --to X --subject Y --body-file Z [--dry]",
                  file=sys.stderr)
            return 1
        return send_mail(opt["--to"], opt["--subject"], opt["--body-file"], dry)
    if cmd == "list":
        list_mail(a[1] if len(a) > 1 else "", as_json)
        return 0
    if cmd == "open":
        if len(a) < 2:
            print("🔴 開く行の目印を書いて。例: csmail.py open \"キャンセル\"",
                  file=sys.stderr)
            return 1
        return open_thread(a[1], a[2] if len(a) > 2 else "", as_json)
    print(f"🔴 知らんコマンド: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
