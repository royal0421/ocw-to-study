# 逐字稿段落合併：把 Whisper 切碎的短句併成段落，只保留每段開頭的時間碼。
#
# 目標：一個時間標記大約 6 行（捲動框內約 220 字），且斷點要落在「一個意思講完」的地方。
#
# 為什麼不能只靠標點：實測這類長時間講課錄影，Whisper 的句尾標點率高度取決於
# 老師該段的講話方式（同一支影片前 10 分鐘 98%、20 分鐘後 1~2%），
# 換模型參數、切短片段都救不回來。所以斷點要靠多個訊號投票。
#
# 斷點評分（超過目標字數後，往後看幾段挑分數最高的邊界）：
#   +100  這一段以句尾標點結束（。！？）——最強訊號
#   + 60  下一段以「起新段落的語氣詞」開頭（那、所以、接下來、第一部分…）
#   + 25  這一段以逗號類標點結束（，、：；）
#   +pause 說話停頓秒數（最弱，只當微調）
TARGET = 120      # 一段的目標字數。窄面板約 25 字/行 -> 約 5 行；寬面板約 38 字/行 -> 約 3 行
LOOKAHEAD = 6     # 超過目標後，最多往後看幾段來挑最佳斷點
CEILING = 200     # 安全閥
ENDERS = '。！？'
JOINERS = '，、：；,'

# 這位講者起新段落時的慣用開頭。命中就代表前一段講完了。
STARTERS = (
    '那我們', '那如果', '那接下來', '那基本上', '那當然', '那反過來', '那至於', '那這',
    '接下來', '再來', '所以我們', '所以', '因此', '另外', '除此之外', '換句話說',
    '總而言之', '第一', '第二', '第三', '第四', '首先', '其次', '最後',
    '至於', '在這頁', '這一頁', '這頁', '在這些裡面', '我們可以看到', '我們知道',
    '我們來看', '我們接著', '我要強調', '值得注意', '一般而言', '事實上', '基本上',
    '針對', '關於', '好',
)


def _join(a, b):
    return a + ('' if a[-1:] in ENDERS + JOINERS else '，') + b


def _score(rows, k, n):
    """把 rows[k] 之後當成斷點的分數"""
    s = 0.0
    if rows[k]['text'][-1:] in ENDERS:
        s += 100.0
    elif rows[k]['text'][-1:] in JOINERS:
        s += 25.0
    if k + 1 < n and rows[k + 1]['text'].lstrip().startswith(STARTERS):
        s += 60.0
    s += (rows[k + 1]['start'] - rows[k]['end']) if k + 1 < n else 99.0
    return s


def merge_rows(rows, target=TARGET, lookahead=LOOKAHEAD, ceiling=CEILING):
    """rows: [{'start','end','text'}]（需按時間排序）-> 合併後的同格式清單"""
    if not rows:
        return []
    out, i, n = [], 0, len(rows)
    while i < n:
        cur = {'start': rows[i]['start'], 'end': rows[i]['end'], 'text': rows[i]['text']}
        j = i + 1
        while j < n and len(cur['text']) < target:
            cur['text'] = _join(cur['text'], rows[j]['text'])
            cur['end'] = rows[j]['end']
            j += 1
        if j < n:
            best_k, best_score = j - 1, _score(rows, j - 1, n)
            probe = cur['text']
            for k in range(j, min(j + lookahead, n)):
                probe = _join(probe, rows[k]['text'])
                if len(probe) > ceiling:
                    break
                sc = _score(rows, k, n)
                if sc > best_score:
                    best_score, best_k = sc, k
                if sc >= 100.0:
                    break
            for k in range(j, best_k + 1):
                cur['text'] = _join(cur['text'], rows[k]['text'])
                cur['end'] = rows[k]['end']
            j = best_k + 1
        out.append(cur)
        i = j
    if len(out) >= 2 and len(out[-1]['text']) < 40:
        out[-2]['text'] = _join(out[-2]['text'], out[-1]['text'])
        out[-2]['end'] = out[-1]['end']
        out.pop()
    return out
