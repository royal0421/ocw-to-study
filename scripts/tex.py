# -*- coding: utf-8 -*-
"""把重點拆解裡的純文字上下標與算式轉成 LaTeX。

2026-09-11 使用者裁示：`.md` 產物裡的算式與上下標一律用 LaTeX（`$...$`），
不要寫成 `V_GS`、`W/L` 這種純文字。這支給所有課共用，寫 explain 時照平常打字即可。

⚠️ 三件一定要守的事（每一條都踩過）：
1. **已經是數學式、程式碼、Wiki 連結的地方不准再動**——先挖成佔位符，最後再放回去。
2. **不要一輪輪 replace**，會改到自己剛產出的東西：`VDD − V_t` 先變 `$V_{DD}-V_t$`，
   後面那條 `V_t -> $V_t$` 又打進去，結果是 `$V_{DD}-$V_t$$`。
3. **式子裡的運算子用 ASCII**（`-`、`>`、`\\ge`）——U+2212 那個全形減號 KaTeX 吃不下。

驗法（呼叫端自己跑）：把 `$...$` 全部挖掉之後 grep `[A-Za-z]_[A-Za-z0-9]`，必須是零；
`$` 的總數必須是偶數。
"""
import re

# 多字元的片語先換。同一個來源字串只會被換一次（佔位符機制），順序只影響「誰先搶到」。
PHRASES = [
    ('W over L', r'$W/L$'),
    # ⚠️ 'W/L' 一定要卡前後不是英數字。沒卡的時候 `(W/LpE)` 裡的 W/L 被換掉，
    #    變成 `($W/L$pE)`（AIC CH2 p.2-12 實際踩過）。
    (re.compile(r'(?<![A-Za-z])W/L(?![A-Za-z])'), r'$W/L$'),
    ('I-V curve', 'I-V curve'),          # 這是名詞不是算式，佔位保護它不被下面的規則咬到
    ('I-V Curve', 'I-V Curve'),
    ('μn', r'$\mu_n$'),
    ('μp', r'$\mu_p$'),
    ('e⁻', r'$e^-$'),
    ('h⁺', r'$h^+$'),
    ('SiO₂', r'$\mathrm{SiO_2}$'),
    ('Si₃N₄', r'$\mathrm{Si_3N_4}$'),
]

# 單一符號的下標：V_GS、I_DS、C_ox、g_m、n_B…
SUB = re.compile(r'(?<![\\$\w])([A-Za-zμβαγλεΔ])_([A-Za-z0-9]{1,6})(?![\w_])')
# 希臘字母單獨出現
GREEK = {'μ': r'$\mu$', 'β': r'$\beta$', 'ε': r'$\varepsilon$', 'λ': r'$\lambda$'}

# 純文字寫法的常見符號（沒有底線，但就是變數）
BARE = ['VDD', 'VSS', 'VCC', 'VEE', 'GND',   # GND 不轉，留著當純文字
        'Cox', 'Tox', 'Vth', 'Vt0', 'Vbe', 'Vce', 'Ic', 'Ib', 'Ie']
BARE_TEX = {
    'VDD': r'$V_{DD}$', 'VSS': r'$V_{SS}$', 'VCC': r'$V_{CC}$', 'VEE': r'$V_{EE}$',
    'Cox': r'$C_{ox}$', 'Tox': r'$T_{ox}$', 'Vth': r'$V_{th}$', 'Vt0': r'$V_{t0}$',
    'Vbe': r'$V_{BE}$', 'Vce': r'$V_{CE}$', 'Ic': r'$I_C$', 'Ib': r'$I_B$', 'Ie': r'$I_E$',
}

# 相鄰兩個數學區之間的運算子 -> 合併成一條式子，運算子換成 LaTeX 認得的寫法
OPS = [('−', '-'), ('-', '-'), ('＞', '>'), ('>', '>'), ('<', '<'), ('=', '='),
       ('≥', r'\ge'), ('≤', r'\le'), ('∝', r'\propto'), ('×', r'\times'),
       ('/', '/'), ('+', '+')]

_PROTECT = re.compile(r'(\$[^$\n]*\$|`[^`\n]*`|\[\[[^\]]*\]\]|!?\[[^\]]*\]\([^)]*\))')


def convert(s, extra=()):
    """回傳把上下標與算式換成 LaTeX 的字串。`extra` 是該課專屬的 (來源, LaTeX) 片語。"""
    slots = []

    def stash(text):
        slots.append(text)
        return '\x00%d\x00' % (len(slots) - 1)

    # 0) 該課專屬的片語要在「保護」之前做——它們多半本來就寫成 `code span`，
    #    先保護就等於把它們鎖住，永遠換不到（實際踩過：`I_DS ∝ V_DS` 整條沒被換）。
    for src, dst in extra:
        if src in s:
            s = s.replace(src, stash(dst))

    # 1) 剩下已經是數學式／程式碼／連結的地方收起來，後面的規則一律不准碰
    s = _PROTECT.sub(lambda m: stash(m.group(0)), s)

    for src, dst in PHRASES:
        if hasattr(src, 'sub'):                 # 已編譯的 regex
            s = src.sub(lambda m, d=dst: stash(d), s)
        elif src in s:
            s = s.replace(src, stash(dst))

    # 2) 純文字寫法的符號
    for w in BARE:
        if w in BARE_TEX:
            s = re.sub(r'(?<![\w$])%s(?![\w])' % re.escape(w),
                       lambda m, t=BARE_TEX[w]: stash(t), s)

    # 2.5) 帶次方的括號式：(V_GS − V_t)² -> $(V_{GS}-V_t)^2$
    #      要在單一下標之前做，否則括號裡先被換掉就湊不成一條式子了。
    def _pow(m):
        inner = m.group(1)
        for a, b in (('−', '-'), ('×', r'\times'), (' ', '')):
            inner = inner.replace(a, b)
        inner = re.sub(r'([A-Za-zμ])_([A-Za-z0-9]{1,6})', r'\1_{\2}', inner)
        sup = {'²': '2', '³': '3'}[m.group(2)]
        return stash('$(%s)^%s$' % (inner, sup))

    s = re.sub(r'\(([A-Za-zμ][A-Za-z0-9_]*(?:\s*[−\-+×]\s*[A-Za-zμ][A-Za-z0-9_]*)+)\)([²³])',
               _pow, s)

    # 2.6) 沒有括號的次方：V_DS² -> $V_{DS}^2$
    s = re.sub(
        r'(?<![\\$\w])([A-Za-zμ])_([A-Za-z0-9]{1,6})([²³])',
        lambda m: stash('$%s_{%s}^%s$'
                        % (m.group(1), m.group(2), {'²': '2', '³': '3'}[m.group(3)])), s)

    # 3) X_YY 形式的下標
    s = SUB.sub(lambda m: stash('$%s_{%s}$' % (m.group(1), m.group(2))), s)

    # 4) 單獨的希臘字母
    for g, t in GREEK.items():
        s = s.replace(g, stash(t))

    # 5) 放回去
    for i, v in enumerate(slots):
        s = s.replace('\x00%d\x00' % i, v)

    # 6) "$V_{GS}$ − $V_t$" -> "$V_{GS} - V_t$"，讀起來才像一條式子
    for op, tex_op in OPS:
        for pat in ('$ %s $' % op, '$%s$' % op):
            s = s.replace(pat, ' %s ' % tex_op if pat.startswith('$ ') else tex_op)
    s = re.sub(r'\$\s+([-+/<>=])\s+\$', r' \1 ', s)
    return s


def audit(s):
    """回傳 (剩下的純文字下標, $ 的總數)。呼叫端拿來當驗收條件。"""
    bare = re.sub(r'\$[^$\n]*\$', '', s)
    return re.findall(r'[A-Za-z]_[A-Za-z0-9]', bare), s.count('$')
