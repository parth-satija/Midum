"""Live Notes -- safe Markdown -> LaTeX conversion and the live-notes document template.

The live-notes model writes its notes as lightweight Markdown (headings, bullets, tables, code, ``$math$``).
This module turns that into LaTeX for a MidumDoc.  One LaTeX error means MidumDocs shows NO PDF at all, and
the text comes from a model that is reading untrusted screen / audio content, so the converter is strict:

* everything outside math / code is escaped, so plain text can never inject a macro;
* math is passed through ONLY if it uses a whitelist of macros/environments and passes a few structural
  checks (balanced braces, no stray ``%``/``#``/``&``, no text-mode specials inside ``\\text{}``...).
  Anything else is shown literally in ``\\texttt`` instead of risking a compile error;
* non-Latin characters (which pdfLaTeX cannot typeset) are mapped to macros or replaced by ``?``.

Pure Python, no Midum imports -- unit-testable on its own.
"""
import re
import unicodedata

END_MARKER = "% <<LIVE-NOTES-END>>"     # new notes are inserted just above this line
MAX_LIST_DEPTH = 4                       # LaTeX refuses to nest lists deeper than 4

PREAMBLE = r"""\documentclass[11pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{enumitem}
\usepackage{array}
\usepackage{tabularx}
\setlist{itemsep=2pt,topsep=3pt,parsep=0pt}
\setlength{\parindent}{0pt}
\setlength{\parskip}{5pt}
\setcounter{secnumdepth}{0}
\sloppy
"""


# ── character handling ───────────────────────────────────────────────────────────────────
_GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "ζ": "zeta", "η": "eta",
    "θ": "theta", "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu", "µ": "mu", "ν": "nu", "ξ": "xi",
    "π": "pi", "ρ": "rho", "σ": "sigma", "ς": "varsigma", "τ": "tau", "υ": "upsilon", "φ": "phi",
    "ϕ": "varphi", "χ": "chi", "ψ": "psi", "ω": "omega", "ϵ": "varepsilon", "ϑ": "vartheta",
    "Γ": "Gamma", "Δ": "Delta", "Θ": "Theta", "Λ": "Lambda", "Ξ": "Xi", "Π": "Pi", "Σ": "Sigma",
    "Υ": "Upsilon", "Φ": "Phi", "Ψ": "Psi", "Ω": "Omega",
}
_GREEK_LATIN = {"Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N",
                "Ο": "O", "Ρ": "P", "Τ": "T", "Χ": "X", "ο": "o"}

# char -> math-mode macro (used inside $...$ directly, and as $macro$ in running text)
_MATH_SYMS = {c: "\\" + n for c, n in _GREEK.items()}
_MATH_SYMS.update({
    "→": r"\rightarrow", "←": r"\leftarrow", "↔": r"\leftrightarrow", "⇒": r"\Rightarrow",
    "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow", "↑": r"\uparrow", "↓": r"\downarrow", "↦": r"\mapsto",
    "≥": r"\geq", "≤": r"\leq", "≠": r"\neq", "≈": r"\approx", "≡": r"\equiv", "∼": r"\sim",
    "≪": r"\ll", "≫": r"\gg", "≅": r"\cong",
    "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div", "·": r"\cdot", "⋅": r"\cdot", "∘": r"\circ",
    "∗": r"\ast", "−": "-",
    "∞": r"\infty", "√": r"\surd", "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∮": r"\oint",
    "∂": r"\partial", "∇": r"\nabla",
    "∈": r"\in", "∉": r"\notin", "⊂": r"\subset", "⊆": r"\subseteq", "⊃": r"\supset", "⊇": r"\supseteq",
    "∪": r"\cup", "∩": r"\cap", "∅": r"\emptyset",
    "∀": r"\forall", "∃": r"\exists", "¬": r"\neg", "∧": r"\wedge", "∨": r"\vee", "∝": r"\propto",
    "∴": r"\therefore", "∵": r"\because",
    "ℏ": r"\hbar", "ℓ": r"\ell", "ℝ": r"\mathbb{R}", "ℤ": r"\mathbb{Z}", "ℕ": r"\mathbb{N}",
    "ℚ": r"\mathbb{Q}", "ℂ": r"\mathbb{C}",
    "′": "'", "″": "''", "°": r"^{\circ}",
    "⁰": "^{0}", "¹": "^{1}", "²": "^{2}", "³": "^{3}", "⁴": "^{4}", "⁵": "^{5}", "⁶": "^{6}",
    "⁷": "^{7}", "⁸": "^{8}", "⁹": "^{9}", "⁺": "^{+}", "⁻": "^{-}",
    "₀": "_{0}", "₁": "_{1}", "₂": "_{2}", "₃": "_{3}", "₄": "_{4}", "₅": "_{5}", "₆": "_{6}",
    "₇": "_{7}", "₈": "_{8}", "₉": "_{9}", "₊": "_{+}", "₋": "_{-}",
    "½": r"\frac{1}{2}", "¼": r"\frac{1}{4}", "¾": r"\frac{3}{4}",
    "✓": r"\checkmark", "✔": r"\checkmark",
})

# char -> text-mode replacement (no math needed)
_TEXT_SYMS = {
    "—": "---", "–": "--", "‑": "-", "‐": "-", "‒": "--", "―": "---",
    "‘": "`", "’": "'", "‚": ",", "“": "``", "”": "''", "„": ",,", "«": "<<", "»": ">>",
    "…": r"\ldots{}", "•": r"\textbullet{}", "●": r"\textbullet{}", "▪": r"\textbullet{}",
    "◦": r"\textbullet{}", "‣": r"\textbullet{}",
    "\u00a0": "~", "\u2009": r"\,", "\u202f": r"\,", "\u2002": " ", "\u2003": " ", "\u2007": " ",
    "\u200b": "", "\u200c": "", "\u200d": "", "\u2060": "", "\ufeff": "", "\u00ad": "",
    "€": r"\texteuro{}", "™": r"\texttrademark{}", "✗": "x", "✘": "x", "★": "*", "☆": "*",
    "\t": " ",
}

_ASCII_FOLD = {"—": "--", "–": "-", "‑": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...",
               "•": "*", "→": "->", "←": "<-", "⇒": "=>", "≥": ">=", "≤": "<=", "≠": "!=", "×": "x",
               "\u00a0": " ", "\t": "    "}


def _is_control(ch):
    o = ord(ch)
    return o < 32 and ch not in "\n\t"


def _uni_text(ch):
    """A single non-ASCII character in running text."""
    if ch in _TEXT_SYMS:
        return _TEXT_SYMS[ch]
    if ch in _MATH_SYMS:
        return "$" + _MATH_SYMS[ch] + "$"
    if ch in _GREEK_LATIN:
        return _GREEK_LATIN[ch]
    if ord(ch) < 0x180:                      # Latin-1 + Latin Extended-A: fine with T1 + utf8
        return ch
    cat = unicodedata.category(ch)
    if cat in ("So", "Sk", "Cf", "Mn", "Me", "Cc", "Cs", "Co", "Cn"):
        return ""                            # emoji, symbols, joiners, combining marks: drop
    ascii_ = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
    return ascii_ if ascii_ else "?"


_SPECIAL = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def escape_text(s):
    """Escape plain text for LaTeX running text (no markdown processing)."""
    out = []
    for ch in str(s):
        if ch in _SPECIAL:
            out.append(_SPECIAL[ch])
        elif ord(ch) < 128:
            out.append("" if _is_control(ch) else ch)
        else:
            out.append(_uni_text(ch))
    return "".join(out)


def _fold(s):
    """Text for a verbatim block: ASCII-ish, never fails."""
    out = []
    for ch in s:
        if ord(ch) < 128:
            out.append("" if _is_control(ch) else ch)
        elif ch in _ASCII_FOLD:
            out.append(_ASCII_FOLD[ch])
        elif ch in _GREEK:
            out.append(_GREEK[ch])
        elif ord(ch) < 0x180:
            out.append(ch)
        else:
            a = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
            out.append(a if a else "?")
    return "".join(out)


_MD_ESCAPES = re.compile(r"\\([\\`*_{}\[\]()#+\-.!$%&~^|<>])")


def _unescape_md(s):
    return _MD_ESCAPES.sub(r"\1", s)


# ── math safety ──────────────────────────────────────────────────────────────────────────
_ALLOWED_MACROS = frozenset("""
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi varpi rho
varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi
Psi Omega hbar ell Re Im partial nabla infty emptyset varnothing aleph wp
sum prod int iint iiint oint lim limsup liminf sup inf max min arg det dim exp ln log lg sin cos tan cot sec
csc sinh cosh tanh coth arcsin arccos arctan gcd deg ker hom Pr bmod pmod mod
leq le geq ge neq ne approx equiv sim simeq cong propto ll gg prec succ subset supset subseteq supseteq in
notin ni perp parallel mid nmid
pm mp times div cdot circ ast star bullet oplus ominus otimes odot cup cap setminus wedge vee land lor lnot neg
to rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow mapsto longrightarrow longleftarrow
longleftrightarrow Longrightarrow Longleftarrow Longleftrightarrow uparrow downarrow updownarrow
rightharpoonup rightleftharpoons iff implies impliedby hookrightarrow
forall exists nexists therefore because angle triangle square checkmark dots ldots cdots vdots ddots prime
frac dfrac tfrac sqrt binom dbinom overline underline overbrace underbrace vec hat bar tilde dot ddot
widehat widetilde check breve acute grave
mathrm mathbf mathit mathsf mathtt mathcal mathbb mathfrak boldsymbol text textbf textit textrm operatorname
left right big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr langle rangle lfloor rfloor lceil rceil lvert rvert
lVert rVert vert Vert
quad qquad not stackrel overset underset substack limits nolimits displaystyle textstyle scriptstyle mathop
mathbin mathrel hline begin end
""".split())

_ALLOWED_ENVS = frozenset("matrix pmatrix bmatrix Bmatrix vmatrix Vmatrix cases aligned gathered array".split())
_TEXT_MACROS = re.compile(r"\\(?:text|textbf|textit|textrm)\{([^{}]*)\}")
_CS_WORD = re.compile(r"\\([A-Za-z]+)")
_ENV = re.compile(r"\\(begin|end)\{([^{}]*)\}")


def _balanced(s):
    depth, i, n = 0, 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


def _math_ok(src, display):
    if not src or len(src) > (3000 if display else 800):
        return False
    if "^^" in src or "\n\n" in src or "$" in src.replace("\\$", ""):
        return False
    if re.search(r"(?<!\\)[%#]", src):
        return False
    if not _balanced(src):
        return False
    for m in _CS_WORD.finditer(src):
        if m.group(1) not in _ALLOWED_MACROS:
            return False
    envs, stack = _ENV.findall(src), []
    for kind, name in envs:
        if name not in _ALLOWED_ENVS:
            return False
        if kind == "begin":
            stack.append(name)
        elif not stack or stack.pop() != name:
            return False
    if stack:
        return False
    has_env = bool(envs)
    if not has_env and (re.search(r"(?<!\\)&", src) or "\\\\" in src or "\\hline" in src):
        return False                         # alignment characters only make sense inside an environment
    if len(re.findall(r"\\left(?![A-Za-z])", src)) != len(re.findall(r"\\right(?![A-Za-z])", src)):
        return False
    for m in _TEXT_MACROS.finditer(src):     # text mode: _ ^ & # % ~ $ \ are all errors there
        if re.search(r"[_^&#%~$\\]", m.group(1)):
            return False
    return True


def _math_unicode(src):
    out = []
    for ch in src:
        if ord(ch) < 128:
            out.append("" if _is_control(ch) else ch)
        elif ch in _MATH_SYMS:
            m = _MATH_SYMS[ch]
            out.append(m + (" " if m[-1:].isalpha() else ""))
        elif ch in _TEXT_SYMS and _TEXT_SYMS[ch] in ("", " ", "~"):
            out.append(" ")
        else:
            out.append("?")
    return "".join(out)


def _math(src, display):
    original = src
    src = _math_unicode(src.strip())
    if _math_ok(src, display):
        return ("\\[" + src + "\\]") if display else ("$" + src + "$")
    delim = "$$" if display else "$"
    return r"\texttt{" + escape_text(delim + original.strip() + delim) + "}"


# ── inline Markdown ──────────────────────────────────────────────────────────────────────
_INLINE = re.compile(
    r"(?P<dmath>\$\$(?P<dm>.+?)\$\$)"
    r"|(?P<dmath2>\\\[(?P<dm2>.+?)\\\])"
    r"|(?P<imath2>\\\((?P<im2>.+?)\\\))"
    r"|(?P<imath>(?<![\\$])\$(?![\s$])(?P<im>(?:\\.|[^$\\\n])+?)(?<![\s\\])\$(?!\d))"
    r"|(?P<code>`(?P<cd>[^`\n]+)`)"
    r"|(?P<bold>\*\*(?P<bd>(?!\s).+?(?<!\s))\*\*)"
    r"|(?P<ital>(?<![\w*])\*(?![\s*])(?P<it>[^*\n]+?)(?<![\s*])\*(?![\w*]))"
    r"|(?P<ital2>(?<![\w\\])_(?![\s_])(?P<it2>[^_\n]+?)(?<![\s_])_(?!\w))",
    re.S,
)


def _plain(gap):
    return escape_text(_unescape_md(gap))


def inline_to_latex(text, _depth=0):
    """Inline Markdown (bold, italic, code, math) -> LaTeX running text."""
    text = str(text)
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(_plain(text[pos:m.start()]))
        if m.group("dmath"):
            out.append(_math(m.group("dm"), True))
        elif m.group("dmath2"):
            out.append(_math(m.group("dm2"), True))
        elif m.group("imath2"):
            out.append(_math(m.group("im2"), False))
        elif m.group("imath"):
            out.append(_math(m.group("im"), False))
        elif m.group("code"):
            out.append(r"\texttt{" + escape_text(m.group("cd")) + "}")
        elif m.group("bold"):
            inner = inline_to_latex(m.group("bd"), _depth + 1) if _depth < 4 else _plain(m.group("bd"))
            out.append(r"\textbf{" + inner + "}")
        elif m.group("ital"):
            inner = inline_to_latex(m.group("it"), _depth + 1) if _depth < 4 else _plain(m.group("it"))
            out.append(r"\emph{" + inner + "}")
        elif m.group("ital2"):
            inner = inline_to_latex(m.group("it2"), _depth + 1) if _depth < 4 else _plain(m.group("it2"))
            out.append(r"\emph{" + inner + "}")
        pos = m.end()
    if pos < len(text):
        out.append(_plain(text[pos:]))
    return "".join(out)


# ── block Markdown ───────────────────────────────────────────────────────────────────────
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEAD = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_HEADING_MACROS = {1: r"\section*", 2: r"\subsection*", 3: r"\subsubsection*"}
MAX_TABLE_COLS = 8


def _split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _table(rows):
    """rows[0] = header. Returns LaTeX lines, or None when the table is too wide/odd to typeset."""
    ncols = max(len(r) for r in rows)
    if ncols < 1 or ncols > MAX_TABLE_COLS:
        return None
    spec = "|" + "X|" * ncols
    out = [r"\par\smallskip\noindent", r"{\small\begin{tabularx}{\linewidth}{" + spec + "}", r"\hline"]
    for i, row in enumerate(rows):
        cells = row + [""] * (ncols - len(row))
        rendered = [inline_to_latex(c) for c in cells]
        if i == 0:
            rendered = [r"\textbf{" + c + "}" if c else c for c in rendered]
        out.append(" & ".join(rendered) + r" \\ \hline")
    out.append(r"\end{tabularx}}\par\smallskip")
    return out


def markdown_to_latex(md):
    """Convert a chunk of notes Markdown to LaTeX (body only, safe to insert into the live-notes document)."""
    lines = str(md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, para, stack = [], [], []           # stack: [(kind, indent)] of open lists

    def flush_para():
        if para:
            out.append(inline_to_latex(" ".join(para)))
            out.append("")
            para.clear()

    def close_lists(keep=0):
        while len(stack) > keep:
            out.append(r"\end{%s}" % stack.pop()[0])

    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip()
        stripped = line.strip()

        # fenced code block
        fm = _FENCE.match(line)
        if fm:
            flush_para()
            close_lists()
            fence, code = fm.group(1), []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                code.append(_fold(lines[i].rstrip()))
                i += 1
            i += 1                                          # closing fence
            body = "\n".join(code).replace("\\end{verbatim}", "\\end {verbatim}")
            out.append("{\\small\\begin{verbatim}\n" + body + "\n\\end{verbatim}}")
            out.append("")
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        hm = _HEAD.match(line)
        if hm:
            flush_para()
            close_lists()
            level = len(hm.group(1))
            macro = _HEADING_MACROS.get(level, r"\paragraph*")
            out.append(macro + "{" + inline_to_latex(hm.group(2)) + "}")
            out.append("")
            i += 1
            continue

        if _HR.match(line):
            flush_para()
            close_lists()
            out.append(r"\par\noindent\rule{\linewidth}{0.4pt}\par")
            out.append("")
            i += 1
            continue

        # table: header row followed by a separator row
        if stripped.startswith("|") and i + 1 < n and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            rows = [_split_row(line)]
            j = i + 2
            while j < n and lines[j].strip().startswith("|"):
                rows.append(_split_row(lines[j]))
                j += 1
            rendered = _table(rows)
            flush_para()
            close_lists()
            if rendered is None:                            # too wide: keep the content, as code
                out.append("{\\scriptsize\\begin{verbatim}\n" + "\n".join(_fold(r.rstrip()) for r in lines[i:j]) + "\n\\end{verbatim}}")
            else:
                out.extend(rendered)
            out.append("")
            i = j
            continue

        qm = _QUOTE.match(line)
        if qm:
            flush_para()
            close_lists()
            paras, cur = [], []
            while i < n and _QUOTE.match(lines[i]):
                content = _QUOTE.match(lines[i]).group(1).strip()
                if content:
                    cur.append(content)
                elif cur:
                    paras.append(" ".join(cur))
                    cur = []
                i += 1
            if cur:
                paras.append(" ".join(cur))
            out.append(r"\begin{quote}")
            out.append("\n\n".join(inline_to_latex(p) for p in paras))
            out.append(r"\end{quote}")
            out.append("")
            continue

        im = _ITEM.match(line)
        if im:
            flush_para()
            indent = len(im.group(1).expandtabs(4))
            kind = "enumerate" if im.group(2)[0].isdigit() else "itemize"
            while stack and indent < stack[-1][1]:
                out.append(r"\end{%s}" % stack.pop()[0])
            if stack and indent == stack[-1][1] and kind != stack[-1][0]:
                out.append(r"\end{%s}" % stack.pop()[0])
            if (not stack or indent > stack[-1][1]) and len(stack) < MAX_LIST_DEPTH:
                stack.append((kind, indent))
                out.append(r"\begin{%s}" % kind)
            out.append(r"\item " + inline_to_latex(im.group(3)))
            i += 1
            continue

        if stack and line[:1] in (" ", "\t"):               # indented continuation of the current list item
            out.append(inline_to_latex(stripped))
            i += 1
            continue

        close_lists()
        para.append(stripped)
        i += 1

    flush_para()
    close_lists()
    return "\n".join(out).strip("\n") + "\n" if out else ""


# ── document helpers ─────────────────────────────────────────────────────────────────────
def make_document(title, started):
    """The complete .tex source of a fresh, empty live-notes document."""
    return (PREAMBLE + "\\title{" + escape_text(title or "Live Notes") + "}\n\\author{}\n\\date{"
            + escape_text(started) + "}\n\n\\begin{document}\n\\maketitle\n\n" + END_MARKER + "\n\\end{document}\n")


def insert_before_end(doc_text, body):
    """Insert ``body`` just above the end marker (falling back to \\end{document}, then the end of the text)."""
    idx = doc_text.rfind(END_MARKER)
    if idx < 0:
        idx = doc_text.rfind(r"\end{document}")
    if idx < 0:
        return doc_text.rstrip("\n") + "\n" + body
    return doc_text[:idx] + body.rstrip("\n") + "\n\n" + doc_text[idx:]


def footer(text):
    """A small italic closing line (used when a session ends)."""
    return "\\par\\medskip\\noindent\\textit{" + escape_text(text) + "}\n"
