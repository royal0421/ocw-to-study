# -*- coding: utf-8 -*-
"""把「整行都是算式」的那種行轉成 LaTeX。

`tex.py` 管的是散在中文句子裡的單一符號（`V_GS` -> `$V_{GS}$`）。
筆記裡還有另一種：整行就是一條式子，寫成 `IC = Is·e^(VBE/UT) = βF·IB`。
那種要整行包起來，不能一個符號一個符號換。

判準（兩個都要）：把 `*`、`>`、`-`、`` ` `` 去掉之後
1. 含有 `=`、`≈`、`∝` 或 `≡`
2. **完全沒有中文字**
中文一旦混進去就不是純算式（例如「> **τF = Qn/iC** ⚠️（投影片寫成…）」），
那種留給 `tex.py` 處理散裝符號，不要硬轉——轉了會把中文包進數學式裡。
"""
import re

CJK = re.compile(r'[\u4e00-\u9fff]')

# 多字元符號先換（長的在前）
SYM = [
    ('≡', r'\equiv'), ('≈', r'\approx'), ('∝', r'\propto'), ('≥', r'\ge'), ('≤', r'\le'),
    ('≪', r'\ll'), ('≫', r'\gg'), ('≠', r'\ne'), ('−', '-'), ('·', r'\cdot'),
    ('×', r'\times'), ('÷', r'\div'), ('√', r'\sqrt'), ('∂', r'\partial'),
    ('Δ', r'\Delta'), ('Ψ', r'\Psi'), ('ψ', r'\psi'), ('τ', r'\tau'), ('β', r'\beta'),
    ('α', r'\alpha'), ('γ', r'\gamma'), ('μ', r'\mu'), ('π', r'\pi'), ('ω', r'\omega'),
    ('λ', r'\lambda'), ('ε', r'\varepsilon'), ('σ', r'\sigma'), ('η', r'\eta'),
    ('θ', r'\theta'), ('Ω', r'\Omega'), ('∞', r'\infty'), ('~', r'\sim'),
    ('°C', r'^\circ\mathrm{C}'), ('²', '^2'), ('³', '^3'),
    ('⁻¹', '^{-1}'), ('⁻²', '^{-2}'), ('⁰', '^0'), ('¹', '^1'),
    ('⁴', '^4'), ('⁵', '^5'), ('⁶', '^6'), ('⁻', '^-'),
]

FUNCS = ('ln', 'log', 'exp', 'sin', 'cos', 'tan', 'max', 'min', 'sqrt')

GREEK_CMDS = ['Delta', 'Psi', 'psi', 'tau', 'beta', 'alpha', 'gamma', 'mu', 'pi',
              'omega', 'lambda', 'varepsilon', 'sigma', 'eta', 'theta', 'Omega']
ALL_CMDS = GREEK_CMDS + ['cdot', 'times', 'div', 'approx', 'propto', 'equiv', 'sqrt',
                         'partial', 'infty', 'sim', 'ge', 'le', 'll', 'gg', 'ne',
                         'circ', 'mathrm'] + list(FUNCS)
# 長的排前面：正則的 | 是「第一個成功就停」，短的排前面會先咬掉 \pi 而放過 \propto
CMD_ALT = '|'.join(sorted(set(ALL_CMDS), key=len, reverse=True))
GREEK_ALT = '|'.join(sorted(set(GREEK_CMDS), key=len, reverse=True))
# XY -> X_{Y}：IC、VBE、Cj0、DnB…（前面不能是反斜線或字母，後面不能再接字母）
VAR = re.compile(r'(?<![\\A-Za-z_{])([A-Za-z])([A-Za-z0-9]{1,4})(?![A-Za-z0-9_}])')
# 行首可能有 `>`、`-`、`1.`、粗體星號
PREFIX = re.compile(r'^(\s*(?:>\s*)?(?:[-*]\s+|\d+\.\s+)?)(.*)$', re.S)


def _var(m):
    whole, head, sub = m.group(0), m.group(1), m.group(2)
    if whole.lower() in FUNCS:
        return whole
    # dQ/dV、dG/dVCE 是微分不是下標
    if head == 'd' and sub[:1].isupper():
        return whole
    return '%s_{%s}' % (head, sub)


def is_formula(line):
    t = re.sub(r'[*>`]', '', line).strip()
    t = re.sub(r'^(?:[-]\s+|\d+\.\s+)', '', t)
    if not t or CJK.search(t):
        return False
    return bool(re.search(r'[=≈∝≡]', t))


def _one(t):
    """轉一條式子（不含 `$`）。"""
    t = t.strip().strip('*').strip()
    if not t:
        return ''
    for a, b in SYM:
        t = t.replace(a, b)
    # ⚠️ 下面兩步一定要用「指令清單 + 長的排前面」去比對，不可以寫成 `\\[a-zA-Z]+`。
    #    那種寫法會回溯：`\beta_{F}` 裡 `[a-zA-Z]+` 先吃掉 beta、發現後面不是字母，
    #    退回成 `bet`，再看到 `a` 是字母就插空白，變成 `\bet a`（實際踩過）。
    # 只有希臘字母會黏下標（\betaF -> \beta_F、\Psi0 -> \Psi_0）。
    # 其他指令不可以——`\sim6` 是「3 ~ 6」不是「sim 的下標 6」。
    t = re.sub(r'\\(%s)([A-Za-z0-9])(?![A-Za-z0-9])' % GREEK_ALT, r'\\\1_{\2}', t)
    t = re.sub(r'\\(%s)(?=[A-Za-z0-9])' % CMD_ALT, r'\\\1 ', t)
    # C\pi -> C_\pi：只有 pi / mu 會這樣當下標（r_π、C_π、C_μ）。
    # `j\omega` 的 omega 不是下標，所以不可以對所有希臘字母一律套。
    t = re.sub(r'(?<![\\{_])([A-Za-z])\\(pi|mu)\b', r'\1_\\\2', t)
    t = re.sub(r'\^(-?\d+)', r'^{\1}', t)              # ^-1 -> ^{-1}
    t = re.sub(r'\^\(([^)]*)\)', r'^{\1}', t)          # e^(VBE/UT) -> e^{VBE/UT}
    t = re.sub(r'\\sqrt\s*\(([^)]*)\)', r'\\sqrt{\1}', t)
    for f in FUNCS:
        t = re.sub(r'(?<!\\)\b%s\b' % f, '\\\\' + f, t)
    t = VAR.sub(_var, t)
    return t


def to_latex(line):
    m = PREFIX.match(line)
    prefix, body = m.group(1), m.group(2).rstrip()
    parts = re.split(r'(，)', body.strip().strip('*'))
    out = []
    for p in parts:
        if p == '，':
            out.append('，')
            continue
        one = _one(p)
        if one:
            out.append('$%s$' % one)
    return prefix + ''.join(out)


def convert_block(text):
    """逐行處理一段 Markdown，只動「整行都是算式」的行。回傳 (新文字, 改了幾行)。"""
    lines, n = [], 0
    for line in text.split('\n'):
        if is_formula(line) and '$' not in line:
            lines.append(to_latex(line))
            n += 1
        else:
            lines.append(line)
    return '\n'.join(lines), n
