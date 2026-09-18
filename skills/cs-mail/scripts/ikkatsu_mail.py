#!/usr/bin/env /usr/bin/python3
"""購入者に同じ文面を1通ずつ出す道具（CSアドレスから）。二重送信は台帳で防ぐ。
使い方: ikkatsu-mail.py <文面.md> <宛先.json> <台帳.json> [--send] [--limit N]
宛先.json = [{"name":"#1001","email":"x@y","customer":{"firstName":..,"lastName":..}}, ...]
既定は下見だけ。--send で送る。"""
import json, os, re, subprocess, sys, tempfile, time
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from namae_check import yobina
CSMAIL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csmail.py")
a = sys.argv[1:]
if len(a) < 3: print(__doc__); sys.exit(1)
kata, atesaki, daicho = a[0], a[1], a[2]
send = "--send" in a
limit = int(a[a.index("--limit")+1]) if "--limit" in a else 9999
src = open(kata, encoding="utf-8").read()
b = re.findall(r"```\n(.*?)\n```", src, re.S)
kenmei, honbun_kata = b[0].strip(), b[1].strip()
dc = json.load(open(daicho)) if os.path.exists(daicho) else {}
ls = json.load(open(atesaki))
dasu = [o for o in ls if o["email"].lower() not in dc][:limit]
print(f"宛先 {len(ls)}人 ／ 送信済み {len(ls)-len([o for o in ls if o['email'].lower() not in dc])} ／ 今回 {len(dasu)}")
def namae(o):
    c = o.get("customer") or {}
    n, _ = yobina(c.get("firstName"), c.get("lastName"))
    return n or "お客様"
if dasu:
    print("1通目:", dasu[0]["name"], namae(dasu[0]), dasu[0]["email"])
    print(honbun_kata.replace("{名前}", namae(dasu[0]))[:200])
if not send: print("（下見だけ）"); sys.exit(0)
ok = ng = 0
for i, o in enumerate(dasu, 1):
    body = honbun_kata.replace("{名前}", namae(o))
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(body + "\n"); p = f.name
    r = subprocess.run(["/usr/bin/python3", CSMAIL, "send", "--to", o["email"], "--subject", kenmei, "--body-file", p],
                       capture_output=True, text=True, timeout=300)
    os.unlink(p)
    oseta = "送信ボタンを押した" in r.stdout
    kakunin = r.returncode == 0
    if oseta:
        dc[o["email"].lower()] = {"order": o["name"], "at": datetime.now().isoformat(timespec="seconds"), "kakunin": kakunin}
        json.dump(dc, open(daicho, "w"), ensure_ascii=False, indent=1)
    if oseta and kakunin: ok += 1
    else: ng += 1
    print(f"{i}/{len(dasu)} {o['name']} {o['email']} {'✅' if oseta and kakunin else ('⚠️押せたが未確認' if oseta else '🔴押せんかった')}", flush=True)
    time.sleep(15)
print(f"完了 ✅{ok} ／ 🔴{ng}")
