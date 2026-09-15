#!/usr/bin/env python
"""ocw-to-study 流程 CLI：上課錄影 mp4 -> 逐頁講解 .md

所有子指令都吃 --root（課程資料夾，產物會放在 <root>/AI/CH<N>/，筆記放在 <root>/）。

  vts.py extract  --root R --ch 2 --video V.mp4     抽投影片（原生解析度）
  vts.py asr      --root R --ch 2 --video V.mp4     轉逐字稿（含繁體化）
  vts.py fix      --root R --ch 2                   套用 ASR 校正表
  vts.py build    --root R --ch 2                   由 content.json 產出筆記
  vts.py preview  --root R --ch 2 [--batch 7]       渲染成 PNG 供確認版面
  vts.py coverage --root R --ch 2 --pdf handout.pdf 對講義 PDF 檢查有無漏頁

依賴：ffmpeg/ffprobe、faster-whisper、zhconv、Pillow、numpy、markdown、PyMuPDF（coverage 用）
"""
import argparse, difflib, glob, json, os, re, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from note_utils import merge_rows

STEP = 2          # 換頁偵測的取樣間隔（秒）
DIFF = 1.0        # 判定換頁的像素差異百分比
PROMPT_DEFAULT = '這是一堂課程錄影，內含中文與英文專有名詞。'

# 轉錄模型與裝置。實測（5 分鐘課程音檔，誤聽詞數越少越好）：
#   CPU medium int8    252s (1.2x)  誤聽 22 處
#   GPU medium fp16     29s (10.3x) 誤聽 23 處
#   GPU large-v2 fp16   51s (5.9x)  誤聽 10 處   <- 最準，且比 CPU 快 5 倍
#   GPU large-v3 fp16   59s (5.1x)  誤聽 18 處
# 有 GPU 就用 large-v2；沒有就退回 CPU medium（large 在 CPU 上太慢）。
GPU_MODEL, GPU_CT = 'large-v2', 'float16'
CPU_MODEL, CPU_CT = 'medium', 'int8'


def pick_device(force=None):
    if force in ('cpu', 'cuda'):
        dev = force
    else:
        try:
            import ctranslate2
            dev = 'cuda' if ctranslate2.get_cuda_device_count() > 0 else 'cpu'
        except Exception:
            dev = 'cpu'
    return (dev, GPU_MODEL, GPU_CT) if dev == 'cuda' else (dev, CPU_MODEL, CPU_CT)


def log(*a):
    # Windows 主控台是 CP950，print 到它編不出來的字（emoji、⚠、①）會丟 UnicodeEncodeError
    # 把整支腳本打斷——訊息本身不重要，不值得為它中止一場轉錄。
    s = ' '.join(str(x) for x in a)
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or 'cp950'
        print(s.encode(enc, 'replace').decode(enc, 'replace'), flush=True)


def chdir_of(root, ch):
    d = os.path.join(root, 'AI', f'CH{ch}')
    os.makedirs(d, exist_ok=True)
    return d


def find_chrome():
    for p in [r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
              os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
              r'C:\Program Files\Microsoft\Edge\Application\msedge.exe']:
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------- extract
def cmd_extract(a):
    from PIL import Image
    import numpy as np
    D = chdir_of(a.root, a.ch)
    raw, slides = os.path.join(D, '_raw'), os.path.join(D, 'slides')
    for p in (raw, slides):
        shutil.rmtree(p, ignore_errors=True)
        os.makedirs(p)
    subprocess.run(['ffmpeg', '-y', '-i', a.video, '-vf', f'fps=1/{STEP},scale=1280:-2',
                    os.path.join(raw, 'f_%05d.png')],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    files = sorted(f for f in os.listdir(raw) if f.endswith('.png'))
    log('取樣張數', len(files))
    kept, prev = [], None
    for f in files:
        t = (int(f[2:7]) - 1) * STEP
        arr = np.asarray(Image.open(os.path.join(raw, f)).convert('L').resize((320, 180)),
                         dtype=np.int16)
        d = 999.0 if prev is None else float((np.abs(arr - prev) > 40).mean() * 100)
        if d > DIFF:
            kept.append((t, round(d, 2)))
            prev = arr
    # 低解析度只用來判斷換頁；輸出的圖用原生解析度逐點精準抽取
    dur = _video_seconds(a.video)
    manifest, med_n, single_n, dots = [], 0, 0, 0
    for i, (t, d) in enumerate(kept, 1):
        dst = f'slide_{i:03d}_t{t // 60:02d}m{t % 60:02d}s.png'
        end = kept[i][0] if i < len(kept) else dur
        used, nd = _grab_slide(a.video, t, end, os.path.join(slides, dst), raw, a.shots)
        med_n += used == 'median'
        single_n += used == 'single'
        dots += nd
        manifest.append({'n': i, 'file': dst, 'sec': t, 'diff': d})
    json.dump(manifest, open(os.path.join(D, 'slides.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    shutil.rmtree(raw, ignore_errors=True)
    size = Image.open(os.path.join(slides, manifest[0]['file'])).size if manifest else None
    log(f'投影片 {len(manifest)} 張，解析度 {size}')
    if a.shots > 1:
        log(f'  其中 {med_n} 張用多幀中位數消掉移動的雷射筆，{single_n} 張因為頁面本身在變動而退回單幀')
    if dots:
        log(f'  另外擦掉 {dots} 個停住不動的雷射光點')


def _video_seconds(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                          '-of', 'default=nw=1:nk=1', path],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out else 0.0


def _grab_slide(video, t, end, dst, tmpdir, shots):
    """抽一張投影片。

    老師的雷射筆會在畫面上留一個紅點，位置隨時講到哪就跑到哪。單抽一幀就是賭
    那一瞬間紅點不在——賭不贏。改成在這一頁停留期間均勻抽 `shots` 幀，逐像素取
    **中位數**：雷射筆每幀位置不同，過半數的幀在該點都是原本的顏色，中位數就把它
    抹掉了；頁面靜止的部分每幀都一樣，中位數等於原畫面，**一個像素都不會變**。

    例外是頁面本身在動（逐項出現的動畫、播影片）——那樣中位數會疊出半透明鬼影。
    所以合成完先比對：任一幀與中位數的差異超過 1% 就判定這頁在變動，退回單幀。
    """
    import numpy as np
    from PIL import Image

    span = end - t
    if shots <= 1 or span < 4:
        subprocess.run(['ffmpeg', '-y', '-ss', str(t + 1), '-i', video, '-frames:v', '1', dst],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 'single', 0
    # 避開換頁前後的轉場：頭 1.5 秒、尾 1.5 秒不取
    lo, hi = t + 1.5, end - 1.5
    times = [lo + (hi - lo) * k / (shots - 1) for k in range(shots)]
    arrs, tmp = [], []
    for k, tt in enumerate(times):
        p = os.path.join(tmpdir, f'_g{k:02d}.png')
        subprocess.run(['ffmpeg', '-y', '-ss', f'{tt:.2f}', '-i', video, '-frames:v', '1', p],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(p):
            arrs.append(np.asarray(Image.open(p).convert('RGB'), dtype=np.uint8))
            tmp.append(p)
    if len(arrs) < 3 or len({x.shape for x in arrs}) != 1:
        for p in tmp:
            os.remove(p)
        subprocess.run(['ffmpeg', '-y', '-ss', str(t + 1), '-i', video, '-frames:v', '1', dst],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 'single', 0
    stack = np.stack(arrs)
    med = np.median(stack, axis=0).astype(np.uint8)
    moving = max(float((np.abs(x.astype(np.int16) - med) > 40).any(axis=2).mean())
                 for x in arrs)
    mid = arrs[len(arrs) // 2]
    for p in tmp:
        os.remove(p)
    out = med if moving <= 0.01 else mid
    out, n_dots = _wipe_laser_dots(out)
    Image.fromarray(out).save(dst)
    return ('median' if moving <= 0.01 else 'single'), n_dots


def _wipe_laser_dots(arr):
    """擦掉中位數之後還留著的雷射光點（老師把筆停在同一處不動時會這樣）。

    判準是**雷射是疊加上去的光**：紅光打在原本的畫面上是加法混色，綠藍分量被抬高
    但不會歸零，所以光點內 `min(R,G,B)` 的中位數很高；投影片自己畫的紅色是實心純色，
    綠藍分量壓到 0。實測（CH1，取中位數合成之後的圖）：

      s8 雷射點       遮罩內 min(RGB) 中位數 **132**（環形，中心 (255,160,144)），fill 0.80
      s7 紅色項目符號  遮罩內 min(RGB) 中位數 **0**（實心 (255,23,0)），fill 0.77
      s7 紅字筆畫      同樣是實心純紅，而且 fill 只有 0.31~0.54

    ⚠️ 試過兩個行不通的判準，不要再走回去：①「中心是不是接近白色」——雷射點中心其實
       只是淡粉紅（min 才 144），而紅字的 bbox 中心因為框到白背景反而 min=255，兩邊
       剛好相反；②「中心比外圈亮」——這顆雷射點是**環形**的，中心與環的 min 值只差 19。
       另外絕不可以把 bbox 外擴之後再看亮度，那會框進周圍的白背景，連實心紅圓都會被
       判成有亮心（試過，CH1 s7 的紅色項目符號因此被誤刪）。

    擦的方式是拿周圍一圈的顏色填回去，而且**只在那一圈顏色夠均勻時才動手**
    （標準差 > 12 就代表旁邊有文字或線條，寧可留著紅點也不要蓋掉內容）。
    """
    import numpy as np

    a = arr.astype(int)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mask = (r > 150) & (r - g > 60) & (r - b > 60)
    if not mask.any() or mask.sum() > 20000:
        return arr, 0

    H, W = mask.shape
    seen = np.zeros_like(mask)
    out = arr.copy()
    wiped = 0
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys, xs):
        if seen[y0, x0]:
            continue
        # flood fill（8 連通），只在稀疏的紅色遮罩上跑
        stack, pts = [(y0, x0)], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            if len(pts) > 900:
                break
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if not 25 <= len(pts) <= 900:
            continue
        yy = [p[0] for p in pts]
        xx = [p[1] for p in pts]
        y1, y2, x1, x2 = min(yy), max(yy), min(xx), max(xx)
        h, w = y2 - y1 + 1, x2 - x1 + 1
        if not (0.4 <= h / w <= 2.5):
            continue
        if len(pts) / (h * w) < 0.65:
            continue                      # 填不滿 bbox → 是筆畫不是光點
        if float(np.median([a[y, x].min() for y, x in pts])) < 60:
            continue                      # 實心純紅 → 投影片自己的元素，不動
        # 連光暈一起擦，範圍外擴
        m = 9
        ry1, ry2 = max(0, y1 - m), min(H, y2 + m + 1)
        rx1, rx2 = max(0, x1 - m), min(W, x2 + m + 1)
        ring = np.concatenate([a[ry1:ry2, rx1:rx1 + 3].reshape(-1, 3),
                               a[ry1:ry2, rx2 - 3:rx2].reshape(-1, 3),
                               a[ry1:ry1 + 3, rx1:rx2].reshape(-1, 3),
                               a[ry2 - 3:ry2, rx1:rx2].reshape(-1, 3)])
        if ring.std(axis=0).max() > 12:
            continue                      # 旁邊有文字或線條 → 不冒險
        out[ry1:ry2, rx1:rx2] = np.median(ring, axis=0).astype(np.uint8)
        wiped += 1
    return out, wiped


# ---------------------------------------------------------------- asr
def cmd_asr(a):
    """分段轉錄 + 可續跑。

    一次餵 60 分鐘音檔的問題：沒有中間存檔，被中止（記憶體守衛、當機）就全部重來。
    改成切成 CHUNK 分鐘一段，每段轉完立刻落檔到 _chunks/，重跑時自動跳過已完成的段。
    代價：段落交界處 condition_on_previous_text 的上下文會斷一次，影響很小。

    ⚠️ CHUNK 預設 5 分鐘，不要調大。實測 large-v2 餵 10 分鐘的 clip 會在後半段
       崩成碎片（段長中位數 1.50s、106/209 段 ≤1.5s、500~600s 平均只剩 5.2 字/段），
       同一支影片切 5 分鐘則三段都正常（中位數 4.00s、≤1.5s 只有 12 段）。
    """
    import zhconv
    from faster_whisper import WhisperModel
    D = chdir_of(a.root, a.ch)
    wav = os.path.join(D, f'CH{a.ch}.wav')
    if not os.path.exists(wav):
        subprocess.run(['ffmpeg', '-y', '-i', a.video, '-vn', '-ac', '1', '-ar', '16000', wav],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                '-of', 'default=nw=1:nk=1', wav],
                               capture_output=True, text=True).stdout.strip())
    CH_SEC = a.chunk * 60
    OVERLAP = 15
    n_chunks = int(dur // CH_SEC) + (1 if dur % CH_SEC else 0)
    cdir = os.path.join(D, '_chunks')
    os.makedirs(cdir, exist_ok=True)
    log(f'音檔 {dur:.0f}s，切成 {n_chunks} 段（每段 {a.chunk} 分鐘）')

    model = None
    for k in range(n_chunks):
        part = os.path.join(cdir, f'part{k:02d}.json')
        if os.path.exists(part):
            log(f'  [{k + 1}/{n_chunks}] 已完成，跳過')
            continue
        # 往前多切 OVERLAP 秒，避免跨切點的字被切一半；轉完再丟掉重疊區的段落
        off = max(0, k * CH_SEC - (OVERLAP if k else 0))
        span = CH_SEC + (OVERLAP if k else 0)
        clip = os.path.join(cdir, f'_clip{k:02d}.wav')
        subprocess.run(['ffmpeg', '-y', '-ss', str(off), '-t', str(span), '-i', wav,
                        '-ac', '1', '-ar', '16000', clip],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if model is None:
            t0 = time.time()
            dev, mdl, ct = pick_device(getattr(a, 'device', None))
            model = WhisperModel(mdl, device=dev, compute_type=ct,
                                 cpu_threads=a.threads or (os.cpu_count() or 8))
            log(f'模型 {mdl} / {dev} / {ct}，載入 {time.time() - t0:.1f}s')
        t0 = time.time()
        # ⚠️ vad_filter=False + condition_on_previous_text=True 是刻意的：
        #    實測句尾標點率 5% -> 79%，段落 17 -> 35 字，專有名詞也更準。代價是耗時約 1.5 倍。
        # ⚠️ 不要把前一段的逐字稿接成 prompt。實測會讓誤差滾雪球：
        #    part00（只給領域提示）句尾標點率 98%，接了前文的 part01 起一路掉到 0%，
        #    part03 甚至出現同一句重複 5 次的 hallucination 迴圈。每段都只給領域提示即可，
        #    10 分鐘的段落內部 condition_on_previous_text 已經足夠維持上下文。
        # ⚠️ 但 condition_on_previous_text=True 在某些課的音軌上會整段卡死：模型每 30 秒
        #    吐一句一模一樣的話然後跳過那個視窗，實測 DIC 2026.09.10 那支 2 小時的影片
        #    有 22.3%（1625 秒）完全沒轉到，而那些時段的音量與正常時段相同
        #    （mean -27 dB / max -4 dB，不是靜音）。同一段音檔關掉這個選項後空白歸零、
        #    字數多 17%。代價是句子被切碎（48 段 -> 139 段）、句尾標點掉光，
        #    但那是 breaks.json 本來就要處理的事，漏掉的 27 分鐘救不回來。
        #    先照預設跑；發現大量 >8 秒空白且該時段確實有人在講話，就加 --no-condition 重跑。
        prompt = a.prompt or PROMPT_DEFAULT
        segs, info = model.transcribe(clip, beam_size=5, vad_filter=False,
                                      condition_on_previous_text=not getattr(a, 'no_condition', False),
                                      initial_prompt=prompt,
                                      # 以下四項專門壓制 condition_on_previous_text 的重複迴圈
                                      repetition_penalty=1.15,
                                      no_repeat_ngram_size=4,
                                      compression_ratio_threshold=2.2,
                                      prompt_reset_on_temperature=0.3)
        rows = [{'start': s.start + off, 'end': s.end + off,
                 'text': zhconv.convert(s.text.strip(), 'zh-tw')} for s in segs]
        # 重疊區的取捨留到合併時做，這裡原封不動落檔。
        # 這一步不能用固定邊界過濾：前一段的 clip 切到邊界就停了，未必真的轉到邊界
        # （實測 part00 只轉到 598.8s），拿 600s 去砍下一段就會在中間吃掉一截
        # （CH2 的 599.4~610.0s 曾整段消失，10.6 秒）。真正的接點只有「前一段實際
        # 轉到哪裡」，而那要等所有段都轉完才知道。
        json.dump(rows, open(part, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        os.remove(clip)
        log(f'  [{k + 1}/{n_chunks}] {len(rows)} 段，耗時 {time.time() - t0:.0f}s -> {os.path.basename(part)}')

    # 逐段接上：接點是「前一段實際轉到哪裡」，不是名目上的 chunk 邊界。
    # 前一段沒轉到的就由下一段補（不漏字），前一段已經涵蓋的就丟掉（不重複）。
    rows, prev_end = [], 0.0
    for k in range(n_chunks):
        part = json.load(open(os.path.join(cdir, f'part{k:02d}.json'), encoding='utf-8'))
        for r in sorted(part, key=lambda r: (r['start'], r['end'])):
            if r['end'] > prev_end + 0.3:
                rows.append(r)
                prev_end = max(prev_end, r['end'])
    rows.sort(key=lambda r: (r['start'], r['end']))
    rows = _dedup_boundary(rows, D)
    _write_transcript(D, rows)
    log(f'合併完成：{len(rows)} 段，涵蓋到 {rows[-1]["end"]:.0f}s / {dur:.0f}s')
    _gap_stats(rows, dur)
    _punct_stats(rows)


def _dedup_boundary(rows, D=None):
    """丟掉 chunk 邊界上被截斷的殘段。

    前一段的 clip 切到邊界就停了，最後一句常常只轉到一半（「我們會發現」），
    而下一段的 clip 從重疊區開始、會把同一句完整轉出來（「我們會發現到在…」）。
    兩段時間重疊、且短的那個是長的那個的開頭 → 丟掉殘段、留完整的那個。

    ⚠️ 不能用嚴格前綴比對：兩段是各自獨立轉錄的，同一句話的誤聽字未必一樣
       （實測 594s 那句一邊聽成「空閥區」、另一邊「空滑區」，startswith 就失效了）。
       改用開頭字串的相似度 >= 0.8。老師真的重複講同一句時兩段不會在時間上重疊，
       所以「時間重疊」這個前提已經把誤判擋掉大半；比不出來的就兩段都留、印警告。
    """
    def bare(s):
        return re.sub(r'[，。、！？?,.!?\s]', '', s)

    def head_same(short, long_):
        s, l = bare(short), bare(long_)
        if len(s) < 3 or len(s) > len(l):
            return False
        return difflib.SequenceMatcher(None, s, l[:len(s)]).ratio() >= 0.8

    out, unresolved = [], []
    for r in rows:
        if out and r['start'] < out[-1]['end'] - 0.3:
            prev = out[-1]
            if head_same(prev['text'], r['text']):
                out[-1] = r          # 前面那個是殘段，換成完整段
                continue
            if head_same(r['text'], prev['text']):
                continue             # 後面這個是殘段，丟掉
            unresolved.append((prev, r))
        out.append(r)
    if len(out) != len(rows):
        log(f'  邊界殘段去重：{len(rows)} -> {len(out)} 段')
    if unresolved:
        # 判不出誰是殘段的（多半是 whisper 自己在同一段裡給了重疊的時間戳），
        # 兩段都留著，落成一張清單。讀逐字稿做人工斷句時對著它看一眼就好，
        # 只印在終端機會被後面的輸出沖掉。
        log(f'  有 {len(unresolved)} 處時間重疊但內容不同，兩段都留著；清單見 _overlaps.txt')
        if D:
            with open(os.path.join(D, '_overlaps.txt'), 'w', encoding='utf-8') as f:
                f.write('時間重疊但內容不同的段落（程式判不出哪個才對，兩段都留著）\n'
                        '讀逐字稿時對照一下，該刪的在 breaks.json 分段時順手處理掉。\n\n')
                for a, b in unresolved:
                    f.write(f'[{int(a["start"]) // 60:02d}:{int(a["start"]) % 60:02d}] '
                            f'{a["start"]:.1f}-{a["end"]:.1f}  {a["text"]}\n')
                    f.write(f'[{int(b["start"]) // 60:02d}:{int(b["start"]) % 60:02d}] '
                            f'{b["start"]:.1f}-{b["end"]:.1f}  {b["text"]}\n\n')
    return out


def _gap_stats(rows, dur):
    """報告時間軸上的空隙。>8 秒的多半是漏字（老師換頁的停頓通常 3~5 秒）。"""
    gaps = []
    prev = 0.0
    for r in rows:
        if r['start'] - prev > 8:
            gaps.append((prev, r['start']))
        prev = max(prev, r['end'])
    if dur - prev > 8:
        gaps.append((prev, dur))
    if gaps:
        log(f'  注意：有 {len(gaps)} 處 >8 秒的空白，可能漏字：'
            + '、'.join(f'{a:.0f}~{b:.0f}s' for a, b in gaps))


def _write_transcript(D, rows):
    def ts(x):
        h, m, s = int(x // 3600), int(x % 3600 // 60), x % 60
        return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')
    json.dump(rows, open(os.path.join(D, 'transcript.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    open(os.path.join(D, 'transcript.srt'), 'w', encoding='utf-8').write(
        '\n'.join(f'{i}\n{ts(r["start"])} --> {ts(r["end"])}\n{r["text"]}\n'
                  for i, r in enumerate(rows, 1)))
    open(os.path.join(D, 'transcript.txt'), 'w', encoding='utf-8').write(
        '\n'.join(f'[{int(r["start"]) // 60:02d}:{int(r["start"]) % 60:02d}] {r["text"]}'
                  for r in rows))


def _punct_stats(rows):
    ends = sum(1 for r in rows if r['text'][-1:] in '。！？')
    log(f'句尾標點率 {100 * ends / max(len(rows), 1):.0f}%'
        f'（低於 50% 代表轉錄參數不對，段落會斷在句子中間）')


# ---------------------------------------------------------------- fix
def cmd_fix(a):
    D = chdir_of(a.root, a.ch)
    raw = os.path.join(D, 'transcript_raw.json')
    if not os.path.exists(raw):
        shutil.copy(os.path.join(D, 'transcript.json'), raw)
        log('備份原始轉錄 ->', os.path.basename(raw))
    rows = json.load(open(raw, encoding='utf-8'))
    pairs = []
    for p in [os.path.join(os.path.dirname(os.path.abspath(__file__)), 'corrections_common.json'),
              os.path.join(D, 'corrections.json')]:
        if os.path.exists(p):
            pairs += [tuple(x) for x in json.load(open(p, encoding='utf-8'))['pairs']]
            log(f'載入 {os.path.basename(p)}')
    hits = {}
    for r in rows:
        t = r['text']
        for old, new in pairs:
            if old in t:
                hits[old] = hits.get(old, 0) + t.count(old)
                t = t.replace(old, new)
        r['text'] = t
    _write_transcript(D, rows)
    log(f'校正表 {len(pairs)} 條，命中 {len(hits)} 條、共 {sum(hits.values())} 處')
    _punct_stats(rows)


# ---------------------------------------------------------------- build
MIN_LINES, MAX_LINES, CPL = 3, 6.4, 25   # 目標 3~6 行；CPL＝捲動框一行約幾個全形字


def disp_lines(s):
    """顯示行數。半形字（英數）只佔半格，不能直接用字數除。"""
    import unicodedata
    w = sum(1.0 if unicodedata.east_asian_width(c) in 'WF' else 0.5 for c in s)
    return w / CPL


def balance(paras):
    """把不足 MIN_LINES 的段落併進較短的鄰段（併完不得超過 MAX_LINES）。
    只做合併、不新增斷點，所以不會產生語意上切錯的邊界。"""
    def join(a, b):
        t = a['text'].rstrip('。') + '，' + b['text'] if a['text'].endswith('。') else a['text'] + b['text']
        return {'start': a['start'], 'end': b['end'], 'text': t}

    changed = True
    while changed and len(paras) > 1:
        changed = False
        for i, p in enumerate(paras):
            if disp_lines(p['text']) >= MIN_LINES:
                continue
            cands = []
            if i > 0:
                cands.append((disp_lines(paras[i - 1]['text']), i - 1))
            if i + 1 < len(paras):
                cands.append((disp_lines(paras[i + 1]['text']), i + 1))
            cands = [c for c in cands if c[0] + disp_lines(p['text']) <= MAX_LINES]
            if not cands:
                continue
            _, j = min(cands)                      # 併進較短的那一邊
            a, b = (j, i) if j < i else (i, j)
            paras[a:b + 1] = [join(paras[a], paras[b])]
            changed = True
            break
    return paras


HR = '<hr style="border:none; border-top:3px solid #8a8a8a; margin:2.6em 0;">'
P_STYLE = 'margin:0.3em 0; font-size:0.92em; line-height:1.5;'
# 2026-09-11：重點拆解改成輸出**原樣的 Markdown**，不再轉成帶行內樣式的 HTML。
# 原因：Obsidian 在 HTML 區塊裡不跑 MathJax，$V_{GS}$ 這類數學式渲染不出來，
# 而使用者要求算式與上下標一律用 LaTeX。改成純 Markdown 之後，數學式、表格、
# 粗體全部交給 Obsidian 自己渲染，原本那組行內樣式表就不需要了。


# 逐字稿面板裡的上下標。**用 `<sub>` 不是 LaTeX**：那一列是 raw HTML（捲動框需要），
# 而 Obsidian 在 HTML 區塊裡不跑 MathJax，寫 `$V_{GS}$` 會原樣顯示成 `$V_{GS}$`。
#
# ⚠️ 只收**不會有別的意思**的符號。`IC` 絕對不能收——DIC 逐字稿裡 15 次全部是
# 「IC 設計」（integrated circuit），不是集極電流。同理 `ID`、`IB` 在口語裡太容易誤中。
SUBS = [
    'VGS', 'VDS', 'VGD', 'VSB', 'VDD', 'VSS', 'VTH', 'VBE', 'VCE', 'VCB', 'VEB',
    'IDS', 'ICQ', 'Cox', 'Cgs', 'Cgd', 'Tox', 'gm',
    'Vgs', 'Vds', 'Vgd', 'Vsb', 'Vbe', 'Vce', 'Vth',
    'VT', 'Vt', 'Vg', 'Vd', 'Vs', 'Vb',
]
# ASR 對大小寫很隨機，同一句話裡會出現 `Vg 跟 vs 之間`。這裡把電壓符號統一成大寫，
# 只收 V 開頭的電壓（`gm`、`rex`、`Cox` 這種本來就該小寫，不可以一律 upper()）。
SUBS_CANON = {w.lower(): w for w in
              ('VGS', 'VDS', 'VGD', 'VSB', 'VDD', 'VSS', 'VTH', 'VBE', 'VCE', 'VCB',
               'VEB', 'VT', 'VG', 'VD', 'VS', 'VB', 'VA')}


def _sub_re(extra=()):
    words = sorted(set(SUBS) | set(extra), key=len, reverse=True)
    return re.compile(r'(?<![A-Za-z0-9])(%s)(?![A-Za-z0-9])'
                      % '|'.join(re.escape(w) for w in words))


_SUB_RE = _sub_re()


def html_subscripts(text, extra=()):
    """`VGS` -> `V<sub>GS</sub>`。只給逐字稿面板用（那裡是 HTML，不能用 LaTeX）。

    `extra` 是**該門課才成立**的符號，寫在 content.json 的 `subs_extra`。
    為什麼要分課：`IC` 在 AIC 的 BJT 章 25 次全是集極電流，
    在 DIC 15 次全是「IC 設計」（integrated circuit）——同一個字串、相反的意思，
    不可能有一張通用白名單。判斷方式：grep 那門課的 transcript 看前後文，不要用猜的。
    """
    rx = _SUB_RE if not extra else _sub_re(extra)

    # 包一層 <span class="tx">，CSS 再把它套上數學字體（Cambria Math 是 Windows 內建的，
    # 長相跟 LaTeX 的 Computer Modern 很接近），這樣逐字稿的符號看起來就跟
    # 重點拆解裡真正的 MathJax 一致——雖然它其實只是 HTML。
    def one(m):
        w = SUBS_CANON.get(m.group(1).lower(), m.group(1))
        return '<span class="tx">%s<sub>%s</sub></span>' % (w[0], w[1:])

    out = rx.sub(one, text)
    # ASR 有時在符號旁邊留了半形空白、有時沒有，於是同一段裡「有的拉開、有的黏著」
    # （使用者實際指出過）。中文那一側的字面空白一律拿掉，**間距統一交給 CSS 的
    # margin-inline**；英文那一側保留空白，不然 `VS Source` 會變成 `VSSource`。
    out = re.sub(r'(</span>)[ 　]+(?=[一-鿿，。、；：？！（）「」])', r'\1', out)
    out = re.sub(r'([一-鿿，。、；：？！（）「」])[ 　]+(?=<span class="tx">)',
                 r'\1', out)
    return out


CJK = r'一-鿿㐀-䶿'
_THIN = ' '                      # thin space，約 1/5 em


def cjk_latin_space(html):
    """在中文與英數之間插入細空白。

    逐字稿裡大量中英夾雜（「其實電子學也都講過 OK」「那沒有 channel 的時候」），
    中文字是方塊、英文是比例字，直接相鄰會擠在一起。CSS 的 `text-autospace`
    目前還不能用，所以直接在文字層插入 U+2009。

    ⚠️ 只處理 `<span class="tx">` **以外**的部分：符號本身已經有 margin，
    再插一個細空白就變兩倍寬。也不要碰全形標點（，。「」），那些字身本來就留了空隙。
    """
    def one(t):
        t = re.sub(r'(?<=[%s])(?=[A-Za-z0-9])' % CJK, _THIN, t)
        return re.sub(r'(?<=[A-Za-z0-9])(?=[%s])' % CJK, _THIN, t)

    parts = re.split(r'(<span class="tx">.*?</span>)', html)
    return ''.join(p if p.startswith('<span') else one(p) for p in parts)


def unwrap(md):
    """把 explain 裡「同一段被折成好幾行」的軟換行攤平成一行。

    ⚠️ 這件事非做不可，不是排版潔癖：**Obsidian 預設「嚴格換行」是關的，
    原始碼裡的單一換行會變成真正的 `<br>`**。而 `text-align: justify`
    永遠不會對齊「換行前的最後一行」——於是每一行都變成自己的最後一行，
    整段看起來完全沒有左右對齊（2026-09-11 實際踩過，繞了兩輪才找到）。

    保留原樣的行：空行、表格 `|`、標題 `#`、分隔線、清單項目與引用的**開頭**。
    清單／引用的續行會被接回它的開頭那一行。
    """
    KEEP = re.compile(r'^\s*(\||#{1,6}\s|---|\*\*\*|```)')
    ITEM = re.compile(r'^\s*(?:[-*+]\s|\d+[.)]\s|>\s?)')
    out = []
    for para in md.split('\n\n'):
        lines = para.split('\n')
        buf = []
        for ln in lines:
            if not ln.strip() or KEEP.match(ln):
                buf.append(ln)
                continue
            if buf and not KEEP.match(buf[-1]) and buf[-1].strip() and not ITEM.match(ln):
                prev = buf[-1].rstrip()
                cur = ln.strip()
                # 兩邊都是半形英數才需要補空格；中文之間直接接起來
                sep = ' ' if (prev[-1:].isascii() and prev[-1:].isalnum()
                              and cur[:1].isascii() and cur[:1].isalnum()) else ''
                buf[-1] = prev + sep + cur
            else:
                buf.append(ln)
        out.append('\n'.join(buf))
    return '\n\n'.join(out)


def cmd_build(a):
    D = chdir_of(a.root, a.ch)
    slides = json.load(open(os.path.join(D, 'slides.json'), encoding='utf-8'))
    trans = json.load(open(os.path.join(D, 'transcript.json'), encoding='utf-8'))
    c = json.load(open(os.path.join(D, 'content.json'), encoding='utf-8'))
    by_n = {s['n']: s for s in c['slides']}
    assert len(slides) == len(c['slides']), ('投影片數與 content.json 不符',
                                             len(slides), len(c['slides']))

    def mmss(s):
        return f'{int(s) // 60:02d}:{int(s) % 60:02d}'

    # 人工審定的斷點（AI/CH<N>/breaks.json）。逐字稿沒有標點時，機械規則一定會
    # 切在句子中間；唯一可靠的做法是讀過之後自己指定段落起點。有審過的頁用人工斷點，
    # 沒審過的退回機械規則。
    bp = os.path.join(D, 'breaks.json')
    manual = set(json.load(open(bp, encoding='utf-8'))['starts']) if os.path.exists(bp) else set()

    def seg_range(i):
        start = slides[i]['sec']
        end = slides[i + 1]['sec'] if i + 1 < len(slides) else 10 ** 9
        return [r for r in trans if start - 6 <= r['start'] < end - 6]

    def idx_range(i):
        start = slides[i]['sec']
        end = slides[i + 1]['sec'] if i + 1 < len(slides) else 10 ** 9
        return [k for k, r in enumerate(trans) if start - 6 <= r['start'] < end - 6]

    def paragraphs(i):
        idx = idx_range(i)
        if not idx:
            return []
        cuts = [k for k in idx if k in manual]
        if not cuts:
            return merge_rows([trans[k] for k in idx])
        # 以人工斷點切段；第一個斷點之前的殘段併進第一段
        bounds = sorted(set([idx[0]] + cuts))
        out = []
        for a, b in zip(bounds, bounds[1:] + [idx[-1] + 1]):
            grp = [trans[k] for k in range(a, b) if k in idx]
            if not grp:
                continue
            txt = grp[0]['text']
            for r in grp[1:]:
                txt += ('' if txt[-1:] in '。！？，、：；,' else '，') + r['text']
            # 段落結尾補句號（Whisper 常常不給），逗號類先去掉再補
            txt = txt.rstrip('，、：；, ')
            if txt[-1:] not in '。！？':
                txt += '。'
            out.append({'start': grp[0]['start'], 'end': grp[-1]['end'], 'text': txt})
        return balance(out)

    ch = a.ch
    out = [f'''---
title: "{c['course_short']} CH{ch} {c['title']} — 逐頁講解"
aliases: [{c['course_short']} CH{ch} 逐頁講解]
tags: [course, {c['course_short']}, handout, 逐頁講解]
cssclasses: [ocw-study]
type: handout-note
course: {c['course']}
chapter: CH{ch}
teacher: {c['teacher']}
source: {c['source']}
---

# {c['course_short']} CH{ch} — {c['title']} 逐頁講解

> 課程：[[{c['course_link']}]]｜授課教師 **{c['teacher']}**｜影片全長 **{c['duration']}**｜共 {len(slides)} 頁（p.{ch}-1 ~ {ch}-{len(slides)}）
> 投影片圖取自影片畫面（原生解析度）；逐字稿由音軌轉錄後校正專有名詞，顯示於投影片右側，**框內可捲動**。
> 〔方括號〕標示轉錄不確定的字詞。純文字逐字稿：`AI/CH{ch}/transcript.txt`

{HR}
''']
    for i, s in enumerate(slides):
        rows = paragraphs(i)
        t0 = mmss(rows[0]['start']) if rows else mmss(s['sec'])
        t1 = mmss(rows[-1]['end']) if rows else mmss(s['sec'])
        ps = '\n'.join(f'<p style="{P_STYLE}"><b>[{mmss(r["start"])}]</b> '
                       f'{cjk_latin_space(html_subscripts(r["text"], c.get("subs_extra", ())))}</p>'
                       for r in rows) or f'<p style="{P_STYLE}">（此頁無對應語音）</p>'
        out.append(f'''## p.{ch}-{s['n']}　{by_n[s['n']]['heading']}

<div style="display:flex; gap:16px; align-items:stretch;">
<div style="flex:0 0 44%;"><img src="AI/CH{ch}/slides/{s['file']}" style="width:100%; display:block; border:1px solid #c8c8c8; border-radius:3px;"></div>
<div style="flex:1 1 auto; min-width:0; position:relative;">
<div style="position:absolute; top:0; left:0; right:0; bottom:0; box-sizing:border-box; display:flex; flex-direction:column; border:1px solid #d8d8d8; border-radius:6px; padding:0 12px; background:rgba(128,128,128,0.06);">
<div style="flex:0 0 auto; padding:8px 0; font-size:0.95em;"><b>🎙 老師逐字稿　{t0} – {t1}</b></div>
<div style="flex:1 1 auto; min-height:0; overflow-y:auto; padding:0 8px 8px 0;">
{ps}
</div>
</div>
</div>
</div>

### 🔍 重點拆解

{unwrap(by_n[s['n']]['explain'].strip())}

{HR}
''')
    out.append(f'回上層：[[{c["course_link"]}]]\n')
    md = '\n'.join(out)

    secs = [x for x in re.split(r'(?=^## )', md, flags=re.M) if x.startswith('## ')]
    assert len(secs) == len(slides), ('章節數', len(secs))
    for i, sec in enumerate(secs, 1):
        row = sec[sec.index('<div style="display:flex'):sec.index('\n\n### 🔍 重點拆解')]
        assert sum(1 for l in row.split('\n') if l.strip() == '') == 0, ('版面區有空行', i)
        assert row.count('<img') == 1 and row.count('overflow-y:auto') == 1, ('結構', i)
        assert row.index('position:relative') < row.index('position:absolute'), ('定位', i)
    covered = sum(len(seg_range(i)) for i in range(len(slides)))
    assert covered == len(trans), ('逐字稿段落遺漏', covered, len(trans))

    dst = os.path.join(a.root, f'{c["course_short"]}_CH{ch}_{c["slug"]}_逐頁講解.md')
    open(dst, 'w', encoding='utf-8').write(md)
    log('WROTE', dst)
    log(f'  頁數 {len(slides)}　逐字稿 {len(trans)} 段全數分配')
    # 逐字稿裡有 <sub>，所以不能用 [^<]+ 去抓整段（會在第一個標籤就停，統計會少算）
    txt = [re.sub(r'<[^>]+>', '', x) for x in re.findall(r'</b> (.*?)</p>', md)]
    ends = sum(1 for x in txt if x.rstrip()[-1:] in '。！？')
    log(f'  時間標記 {len(txt)} 個，其中 {ends} 段（{100 * ends / max(len(txt), 1):.0f}%）結束於句尾標點')


# ---------------------------------------------------------------- preview
CSS = """
:root { color-scheme: light; }
/* ⚠️ 這幾個數字是照 Obsidian 的預設值抄的，**不要為了 PNG 好看去改**。
   預覽的意義是「跟使用者螢幕上看到的同一個畫面」，寬度一改，
   逐字稿的換行位置就整個不一樣，拿來驗版面就沒有意義了（實際被指出過）。
     --file-line-width  700px（「可讀行長」預設開啟）
     ⚠️ 但這個 vault 另有 `ESD-fundamental.css` / `GaN-ESD.css` / `T-coil.css`
     三個 snippet 把 `.markdown-reading-view .markdown-preview-section` 設成
     **max-width: 960px 且沒有限定 class**，等於全 vault 生效 —— 所以實際是 960px。
     查法：grep 所有已啟用的 snippet 找 max-width / file-line-width，
     **不要只看 app.json 的 readableLineLength**（我就是只看那個才對錯的）。
     字級 16px、行高 1.5、Windows 上的字體是 Segoe UI + 微軟正黑
   使用者若改過主題或字級，要回來同步這裡。目前 .obsidian 沒有 theme、
   appearance.json 沒有 baseFontSize、app.json 沒有 readableLineLength ＝ 全預設。 */
body { margin:0; background:#fff; color:#222;
  /* 試用中：英文用 Times New Roman。它沒有中文字，所以中文會自動落到後面的微軟正黑，
     不需要標記哪些字是英文。要還原就把 Times New Roman 拿掉、換回 Segoe UI。 */
  font-family:"Times New Roman","Microsoft JhengHei","Noto Sans TC",sans-serif;
  font-size:16px; line-height:1.5; }
.page { margin:0 auto; padding:28px 0; max-width:854px; }
h1 { font-size:1.9em; margin:.4em 0 .6em; }
h2 { font-size:1.45em; margin:1.4em 0 .5em; padding-bottom:.2em; border-bottom:1px solid #ddd; }
blockquote { border-left:3px solid #b0b0b0; margin:1em 0; padding:.4em 1em;
  background:#f6f6f6; color:#444; font-size:.95em; }
img { max-width:100%; }
.ruler { position:sticky; top:0; background:#fffbe6; border-bottom:1px solid #e6d98a;
  font-size:13px; padding:6px 32px; color:#6b5b00; z-index:9; }
/* 重點拆解改成純 Markdown 之後就沒有行內樣式了，表格／清單的樣式改在這裡給。
   Obsidian 自己有主題樣式，這幾條只影響預覽 PNG。 */
h3 { font-size:1.12em; margin:1.1em 0 .4em; }
/* 跟 Obsidian 的 .ocw-study snippet 對齊：重點拆解左右對齊，逐字稿欄維持靠左 */
.page > p, .page > ul li, .page > ol li, blockquote p {
  text-align:justify; text-justify:inter-ideograph; hanging-punctuation:allow-end; }
p[style*="0.92em"] { text-align:left; text-justify:auto; }
/* 逐字稿裡的 <sub> 不要把行高撐開（捲動框高度是固定的） */
sub, sup { font-size:.72em; line-height:0; position:relative; vertical-align:baseline; }
sub { bottom:-.25em; }
sup { top:-.5em; }
/* 逐字稿的符號套數學字體，看起來跟重點拆解的 MathJax 一致。
   ⚠️ 左右邊距要**不對稱**：斜體字形往右上倒、下標又掛在右下角，右側的視覺空隙
   會被字形本身吃掉。左右都給 0.12em 的話，左邊剛好、右邊還是黏著中文（實際被指出過）。*/
.tx { font-family:"Cambria Math","Latin Modern Math","STIX Two Math",Georgia,serif;
  font-style:italic; margin-left:.08em; margin-right:.26em; }
table { border-collapse:collapse; margin:.6em 0; font-size:.95em; }
th, td { border:1px solid #d5d5d5; padding:4px 9px; text-align:left; }
th { background:#f3f3f3; }
ul, ol { margin:.4em 0; padding-left:1.6em; }
li { margin:.15em 0; }
code { background:#f0f0f0; padding:1px 4px; border-radius:3px; font-size:.93em; }
"""


def cmd_preview(a):
    import markdown
    from PIL import Image
    import numpy as np
    chrome = find_chrome()
    assert chrome, '找不到 Chrome/Edge，無法截圖'
    D = chdir_of(a.root, a.ch)
    cands = glob.glob(os.path.join(a.root, f'*_CH{a.ch}_*_逐頁講解.md'))
    assert len(cands) == 1, ('找不到唯一的筆記檔', cands)
    OUT = os.path.join(D, 'preview')
    # OneDrive 偶爾鎖住資料夾，rmtree 會靜默失敗（ignore_errors），
    # 接著 makedirs 就炸 FileExistsError。用 exist_ok 讓它照樣往下跑。
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(os.path.join(D, 'slides', '*.png')):
        shutil.copy(f, OUT)
    src = open(cands[0], encoding='utf-8').read()
    src = re.sub(r'^---\n.*?\n---\n', '', src, count=1, flags=re.S)
    src = re.sub(r'\[\[([^\]]+)\]\]', r'<a href="#">\1</a>', src)
    src = src.replace(f'src="AI/CH{a.ch}/slides/', 'src="')
    body = markdown.markdown(src, extensions=['md_in_html', 'tables', 'fenced_code'])
    parts = re.split(r'(?=<h2)', body)
    head = '' if parts[0].startswith('<h2') else parts[0]
    secs = [p for p in parts if p.startswith('<h2')]
    log(f'共 {len(secs)} 頁，每批 {a.batch} 頁')
    for b in range(0, len(secs), a.batch):
        grp = secs[b:b + a.batch]
        lo, hi = b + 1, b + len(grp)
        # KaTeX：Obsidian 會渲染 $...$，預覽也要渲染，否則看到的是生的 LaTeX 原始碼、
        # 沒辦法用預覽驗算式對不對。需要連外；連不上就只是退回顯示原始碼，不會失敗。
        css = (CSS.replace('max-width:854px', 'max-width:%dpx' % a.width)
                  .replace('font-size:16px', 'font-size:%dpx' % a.font))
        html = (f'<!doctype html><html lang="zh-TW"><head><meta charset="utf-8">'
                f'<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/'
                f'katex@0.16.9/dist/katex.min.css">'
                f'<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/'
                f'dist/katex.min.js"></script>'
                f'<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/'
                f'dist/contrib/auto-render.min.js" '
                f'onload="renderMathInElement(document.body,{{delimiters:['
                f'{{left:\'$$\',right:\'$$\',display:true}},'
                f'{{left:\'$\',right:\'$\',display:false}}]}})"></script>'
                f'<style>{css}</style></head><body>'
                f'<div class="ruler">CH{a.ch}　p.{a.ch}-{lo} ~ {a.ch}-{hi}</div>'
                f'<div class="page">{head if b == 0 else ""}{"".join(grp)}</div></body></html>')
        hp = os.path.join(OUT, f'part{b // a.batch + 1}.html')
        open(hp, 'w', encoding='utf-8').write(html)
        png = os.path.join(OUT, f'preview_CH{a.ch}_p{lo}-{hi}.png')
        subprocess.run([chrome, '--headless=new', '--disable-gpu', '--hide-scrollbars',
                        # 1020 = 實際正文寬 960px + 兩側留白。改寬就對不上他的畫面。
                        '--force-device-scale-factor=2', f'--window-size={a.width + 60},16000',
                        # 等 KaTeX 從 CDN 載完並渲染，不然截到的是還沒排版的原始 $...$
                        '--virtual-time-budget=8000',
                        f'--screenshot={png}', 'file:///' + hp.replace('\\', '/')],
                       capture_output=True, timeout=300)
        im = Image.open(png).convert('RGB')
        arr = np.asarray(im)
        nz = np.where((arr < 245).any(axis=2).any(axis=1))[0]
        bottom = int(nz.max()) + 24 if len(nz) else im.height
        im.crop((0, 0, im.width, min(bottom, im.height))).save(png)
        log('  OK', os.path.basename(png), f'{im.width}x{bottom}')


# ---------------------------------------------------------------- coverage
def cmd_coverage(a):
    import fitz
    import numpy as np
    from PIL import Image
    D = chdir_of(a.root, a.ch)
    SL = os.path.join(D, 'slides')

    def norm(im):
        arr = np.asarray(im.convert('L').resize((200, 140)), dtype=np.float32)
        arr -= arr.mean()
        s = arr.std()
        return arr / s if s > 1e-6 else arr

    doc = fitz.open(a.pdf)
    pages = []
    for i in range(doc.page_count):
        pm = doc[i].get_pixmap(dpi=60)
        pages.append(norm(Image.frombytes('RGB', [pm.width, pm.height], pm.samples)))
    doc.close()
    files = sorted(f for f in os.listdir(SL) if f.endswith('.png'))
    shots = [norm(Image.open(os.path.join(SL, f))) for f in files]
    log(f'講義 PDF {len(pages)} 頁　影片抽出 {len(shots)} 張　差 {len(pages) - len(shots)}')
    sim = np.zeros((len(pages), len(shots)))
    for i, p in enumerate(pages):
        for j, s in enumerate(shots):
            sim[i, j] = float((p * s).mean())
    log('\nPDF 頁 -> 最相似的影片投影片（用來看對角線是否連續；分數本身不可靠）')
    for i in range(len(pages)):
        j = int(sim[i].argmax())
        log(f'  p.{a.ch}-{i + 1:<3d} -> {files[j][:22]:24s} {sim[i, j]:.3f}')
    log('\n判讀方式：看「PDF 頁 N -> slide N-k」的位移是否從某頁起整體 +1，'
        '那一頁就是影片沒播到的。分數低不代表漏抓（黑白 vs 彩色會壓低分數）。')


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ['extract', 'asr', 'fix', 'build', 'preview', 'coverage']:
        p = sub.add_parser(name)
        p.add_argument('--root', required=True)
        p.add_argument('--ch', required=True)
        if name in ('extract', 'asr'):
            p.add_argument('--video', required=True)
        if name == 'extract':
            p.add_argument('--shots', type=int, default=5,
                           help='每頁抽幾幀取中位數以去掉雷射筆（1＝關閉，回到單幀）')
        if name == 'asr':
            p.add_argument('--prompt', default=None)
            p.add_argument('--chunk', type=int, default=5, help='每段幾分鐘（>5 會讓 large-v2 碎片化，別調大）')
            p.add_argument('--threads', type=int, default=None)
            p.add_argument('--device', choices=['cpu', 'cuda'], default=None,
                           help='不給就自動偵測：有 CUDA 用 large-v2/GPU，否則 medium/CPU')
            p.add_argument('--no-condition', action='store_true',
                           help='關掉 condition_on_previous_text。只有在發現大量 >8 秒空白、'
                                '且該時段確實有人在講話時才用（模型卡在重複迴圈）')
        if name == 'preview':
            p.add_argument('--batch', type=int, default=7)
            p.add_argument('--width', type=int, default=854,
                           help='正文寬度（px），要跟使用者 Obsidian 的實際值一樣，'
                                '否則逐字稿換行位置對不上、預覽就沒有驗證價值。'
                                '量法：Obsidian 按 Ctrl+Shift+I 開 Console 貼一行 '
                                'getComputedStyle 讀 .markdown-preview-sizer 的寬度')
            p.add_argument('--font', type=int, default=16, help='正文字級（px）')
        if name == 'coverage':
            p.add_argument('--pdf', required=True)
    a = ap.parse_args()
    a.root = os.path.abspath(a.root)
    {'extract': cmd_extract, 'asr': cmd_asr, 'fix': cmd_fix,
     'build': cmd_build, 'preview': cmd_preview, 'coverage': cmd_coverage}[a.cmd](a)


if __name__ == '__main__':
    main()
