# -*- coding: utf-8 -*-
"""随机化对拍：用标准库 email 随机生成嵌套报文，与 mime_parser 对拍。"""
import random
import sys

from email.message import EmailMessage

from diff_test import mine_tree, ref_tree

TEXT_CHARS = "abcXYZ 0123\n中文éü\t"


def rand_text(rng, limit=300):
    return "".join(rng.choice(TEXT_CHARS) for _ in range(rng.randrange(limit)))


def make_leaf(rng):
    msg = EmailMessage()
    kind = rng.choice(["text", "html", "bin"])
    if kind in ("text", "html"):
        subtype = "plain" if kind == "text" else "html"
        charset = rng.choice(["utf-8", "gbk", "latin-1"])
        cte = rng.choice(["7bit", "quoted-printable", "base64"])
        text = rand_text(rng)
        for attempt in (text, text.encode(charset, "ignore").decode(charset, "ignore")):
            try:
                msg.set_content(attempt, subtype=subtype, charset=charset, cte=cte)
                break
            except (UnicodeEncodeError, ValueError):
                continue
        else:
            msg.set_content("fallback", subtype="plain")
    else:
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(600)))
        msg.set_content(data, maintype="application", subtype="octet-stream")
        if rng.random() < 0.7:
            fname = rng.choice(["report.pdf", "中文文件.bin", "a b.txt", "x" * 80])
            msg.add_header("Content-Disposition", "attachment", filename=fname)
    return msg


def make_part(rng, depth):
    if depth <= 0 or rng.random() < 0.45:
        return make_leaf(rng)
    msg = EmailMessage()
    msg.set_type("multipart/" + rng.choice(["mixed", "alternative", "related"]))
    for _ in range(rng.randrange(1, 4)):
        msg.attach(make_part(rng, depth - 1))
    return msg


def make_message(rng):
    root = make_part(rng, rng.randrange(1, 4))
    root["Subject"] = rand_text(rng, 60).replace("\n", " ")
    root["From"] = "发件人 <sender@example.com>"
    if rng.random() < 0.5:
        root["X-Long"] = "y" * rng.randrange(200, 6000)
    return root.as_bytes()


def main(rounds=300, seed=1234):
    rng = random.Random(seed)
    failures = 0
    for i in range(rounds):
        raw = make_message(rng)
        mine, ref = mine_tree(raw), ref_tree(raw)
        if mine != ref:
            failures += 1
            print("FAIL round %d (seed=%d)" % (i, seed))
            with open("fuzz_failure_%d.eml" % i, "wb") as fh:
                fh.write(raw)
            print("  mine:", mine)
            print("  ref :", ref)
            if failures >= 3:
                break
    print("%d/%d random messages match reference" % (rounds - failures, rounds))
    return 1 if failures else 0


if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1234
    sys.exit(main(rounds, seed))
