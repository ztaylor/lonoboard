"""The character grid and everything about drawing on it: cells, CP437 drawing helpers,
the CGA/VGA palette, and encoding frames as ANSI (diffed, for telnet and the web terminal)."""
import re

from lonoboard.util import clean  # noqa: F401  (re-exported for screens)

BLANK = (' ', 7, 0, None)
BLINK = 16          # OR'd into a foreground colour: blinking text
SVC_COLORS = [11, 14, 13, 10, 12, 9, 15, 3, 6, 5, 2]
SPIN = '░▒▓█▓▒'

# Colours are CGA/VGA palette indices (0 black, 1 blue, 2 green, 3 cyan, 4 red, 5 magenta,
# 6 brown, 7 grey, +8 bright). ANSI numbers the low eight differently (1 red, 4 blue...).
CGA2ANSI = [0, 4, 2, 6, 1, 5, 3, 7]
VGA_RGB = [(0, 0, 0), (0, 0, 170), (0, 170, 0), (0, 170, 170), (170, 0, 0), (170, 0, 170), (170, 85, 0), (170, 170, 170),
           (85, 85, 85), (85, 85, 255), (85, 255, 85), (85, 255, 255), (255, 85, 85), (255, 85, 255), (255, 255, 85), (255, 255, 255)]


def _mono(base):
    out = []
    for i, (r, g, b) in enumerate(VGA_RGB):
        k = 0 if i == 0 else .14 + .86 * (.299 * r + .587 * g + .114 * b) / 255
        out.append(tuple(round(c * k) for c in base))
    return out


PAL_RGB = {'vga': VGA_RGB, 'amber': _mono((255, 176, 0)), 'green': _mono((60, 255, 110))}
_LUM = [0, .07, .4, .47, .2, .27, .45, .67, .33, .4, .73, .8, .53, .6, .93, 1]
MONO = {'amber': (0, 6, 14), 'green': (0, 2, 10)}          # 16-colour approximation of the mono palettes
_CUBE = [0, 95, 135, 175, 215, 255]


def xterm256(rgb):
    """Nearest xterm-256 colour from the 6x6x6 cube or the grey ramp (both untouched by terminal themes)."""
    near = lambda c: min(range(6), key=lambda i: abs(_CUBE[i] - c))
    ci = [near(c) for c in rgb]
    cube = 16 + 36 * ci[0] + 6 * ci[1] + ci[2]
    cube_err = sum((_CUBE[i] - c) ** 2 for i, c in zip(ci, rgb))
    g = max(0, min(23, round((sum(rgb) / 3 - 8) / 10)))
    grey_err = sum((8 + 10 * g - c) ** 2 for c in rgb)
    return 232 + g if grey_err < cube_err else cube


# ---------- small helpers ----------

def fit(s, n):
    s = '' if s is None else str(s)
    return s if len(s) <= n else s[:max(0, n - 1)] + '»'


def padr(s, n):
    return fit(s, n).ljust(n)


def padl(s, n):
    return fit(s, n).rjust(n)


def dur(s):
    if s is None:
        return '-'
    s = max(0, int(s))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f'{d}d {h}h' if d else f'{h}h {m:02d}m' if h else f'{m}m {s % 60:02d}s' if m else f'{s}s'


def gib(mib):
    return '-' if mib is None else f'{mib / 1024:.1f}g'


def home(p):
    return re.sub(r'^/home/[^/]+', '~', str(p))


def strip_scheme(u):
    return re.sub(r'^https?://', '', str(u))


def wrap(text, w):
    lines = []
    for para in str(text).split('\n'):
        line = ''
        for word in para.split(' '):
            while len(word) > w:
                if line:
                    lines.append(line)
                    line = ''
                lines.append(word[:w])
                word = word[w:]
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= w:
                line += ' ' + word
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


# ---------- the character grid ----------

class Screen:
    __slots__ = ('w', 'rows')

    def __init__(self, w):
        self.w, self.rows = w, []

    @property
    def h(self):
        return len(self.rows)

    def row(self, y):
        while len(self.rows) <= y:
            self.rows.append([BLANK] * self.w)
        return self.rows[y]

    def put(self, x, y, text, f=7, b=0, l=None):
        r = self.row(y)
        text = str(text)
        if not text.isprintable():            # control and format characters never reach a terminal
            text = ''.join(ch for ch in text if ch.isprintable())
        for ch in text:
            if 0 <= x < self.w:
                r[x] = (ch, f, b, l)
            x += 1
        return x

    def segs(self, x, y, segs):
        for s in segs:
            x = self.put(x, y, s[0], s[1] if len(s) > 1 else 7, s[2] if len(s) > 2 else 0, s[3] if len(s) > 3 else None)
        return x

    def link(self, y, x0, x1, l):
        r = self.row(y)
        for x in range(max(0, x0), min(x1, self.w)):
            c = r[x]
            r[x] = (c[0], c[1], c[2], l)

    def fill_bg(self, y, x0, x1, b):
        r = self.row(y)
        for x in range(max(0, x0), min(x1, self.w)):
            c = r[x]
            r[x] = (c[0], c[1], b, c[3])

    def blit(self, src, x, y):
        for j, r in enumerate(src.rows):
            dst = self.row(y + j)
            for i, c in enumerate(r):
                if 0 <= x + i < self.w:
                    dst[x + i] = c


def seg_len(segs):
    return sum(len(s[0]) for s in segs)


def center_segs(s, y, segs):
    s.segs(max(0, (s.w - seg_len(segs)) // 2), y, segs)


def pack(items, w, sep):
    lines, cur = [], []
    for it in items:
        nxt = cur + [sep] + it if cur else list(it)
        if cur and seg_len(nxt) > w:
            lines.append(cur)
            cur = list(it)
        else:
            cur = nxt
    if cur:
        lines.append(cur)
    return lines


def hdr(s, y, title, meta=''):
    """Section title: a blue band dissolving at both ends, then a rule."""
    x = s.segs(0, y, [('░▒▓', 1), (f' {title} ', 15, 1), ('▓▒░', 1)])
    m = f' {meta} ' if meta else ''
    x = s.put(x + 1, y, '─' * max(0, s.w - x - 1 - len(m) - 3), 8)
    if m:
        x = s.put(x, y, m, 3)
    s.put(x, y, '──·', 8)


def leader(label, w):
    return [(label, 3), (' ' + '.' * max(1, w - len(label) - 2) + ' ', 8)]


def fade_rule(s, y, x0, w, ch='─'):
    for i in range(w):
        d = abs(i - (w - 1) / 2) / (w / 2)
        f = 7 if d < .25 else 8 if d < .55 else 1 if d < .85 else None
        if f is not None:
            s.put(x0 + i, y, ch, f)


def boxed(w, title, lines, min_h=0):
    s = Screen(w)
    x = s.segs(0, 0, [('┌─┤', 1), (f' {title} ', 15), ('├', 1)])
    s.put(x, 0, '─' * max(0, w - x - 1) + '┐', 1)
    body = lines + [[]] * max(0, min_h - len(lines))
    for j, segs in enumerate(body):
        s.put(0, j + 1, '│', 1)
        r = s.segs(2, j + 1, segs)
        if r > w - 2:
            s.put(w - 3, j + 1, '»', 8)
        s.put(w - 2, j + 1, ' │', 1)
    s.put(0, len(body) + 1, '└' + '─' * (w - 2) + '┘', 1)
    return s


# ---------- windows ----------

def frame_box(MW, title, lines, foot, height=None, scroll=0):
    """Blue double-line window; lines beyond the height scroll. Returns (screen, scroll used)."""
    body_h = len(lines) if height is None else max(1, height - 2)
    scroll = max(0, min(scroll, max(0, len(lines) - body_h)))
    shown = lines[scroll:scroll + body_h]
    more = scroll + body_h < len(lines)
    s = Screen(MW)
    x = s.segs(0, 0, [('╔═╡', 15, 1), (f' {title} ', 14, 1), ('╞', 15, 1)])
    s.put(x, 0, '═' * max(0, MW - x - 1) + '╗', 15, 1)
    y = 1
    for ln in shown + [[]] * (body_h - len(shown)):
        if isinstance(ln, dict):
            x = s.segs(0, y, [('╟─', 15, 1), (f" {ln['divider']} ", 11, 1)])
            s.put(x, y, '─' * max(0, MW - x - 1) + '╢', 15, 1)
        else:
            s.put(0, y, '║ ', 15, 1)
            s.fill_bg(y, 1, MW - 1, 1)
            s.segs(2, y, [(t[0], t[1] if len(t) > 1 else 7, t[2] if len(t) > 2 else 1) + tuple(t[3:]) for t in ln])
            s.put(MW - 2, y, ' ║', 15, 1)
        y += 1
    foot = foot + (' · [pgdn] more' if more else '')
    x = s.segs(0, y, [('╚═╡', 15, 1), (f' {foot} ', 7, 1), ('╞', 15, 1)])
    s.put(x, y, '═' * max(0, MW - x - 1) + '╝', 15, 1)
    return s, scroll


# ---------- ANSI encoding ----------

def link_target(sess, l):
    if not l:
        return None
    if l.startswith('u:'):
        url = l[2:]
        ok = url.startswith(('http://', 'https://')) and url.isprintable()
        return url if ok and (sess.kind == 'web' or sess.utf8) else None
    if l.startswith('k:') and sess.kind == 'web':
        return 'lonoboard:' + l[2:]
    return None


def attr_of(sess, f, b, blink):
    if sess.kind == 'telnet':
        if sess.prefs['pal'] in MONO and (sess.ansi_bbs or sess.colors == 'theme'):
            lv = MONO[sess.prefs['pal']]
            mono = lambda c: lv[0] if _LUM[c] < .1 else lv[1] if _LUM[c] < .5 else lv[2]
            f, b = mono(f), mono(b)
        if not sess.prefs['ice'] and b >= 8:
            b, blink = b - 8, True
        if sess.ansi_bbs and sess.prefs['ice'] and blink:
            blink = False                      # with iCE on, the blink bit means bright background
    return f, b, blink


def sgr(sess, a):
    f, b, blink = a
    if sess.ansi_bbs:                          # classic ANSI-BBS: bold = bright fg, blink = bright bg (iCE)
        p = ['0'] + (['1'] if f >= 8 else []) + [str(30 + CGA2ANSI[f % 8])]
        p += ['5'] if (b >= 8 or blink) else []
        p += [str(40 + CGA2ANSI[b % 8])]
    elif sess.colors == 'theme':               # the terminal's own 16 colours (its theme decides the shades)
        p = ['0', str((30 if f < 8 else 90) + CGA2ANSI[f % 8]), str((40 if b < 8 else 100) + CGA2ANSI[b % 8])]
        p += ['5'] if blink else []
    else:                                      # exact VGA colours, whatever the terminal theme
        pal = PAL_RGB.get(sess.prefs['pal'], VGA_RGB)
        if sess.colors == 'exact':
            p = ['0', '38;2;%d;%d;%d' % pal[f], '48;2;%d;%d;%d' % pal[b]]
        else:
            p = ['0', f'38;5;{xterm256(pal[f])}', f'48;5;{xterm256(pal[b])}']
        p += ['5'] if blink else []
    return '\x1b[' + ';'.join(p) + 'm'


def encode(sess, scr, cursor):
    rows = scr.rows
    h, w = len(rows), scr.w
    out = list(sess.pending)
    sess.pending.clear()
    prev = sess.prev
    if prev is None or len(prev) != h or len(prev[0]) != w:
        out.append('\x1b[0m\x1b[2J\x1b[H')
        if (sess.kind == 'web' or sess.utf8) and getattr(sess, 'title', None):
            out.append(f"\x1b]2;{sess.title}\x07")
        prev, sess.attr = None, None
    out.append('\x1b[?25l')
    changed = False
    cur_link = None
    for y, row in enumerate(rows):
        if prev is not None and prev[y] == row:
            continue
        if prev is None:
            i0, i1 = 0, w - 1
        else:
            pr = prev[y]
            i0 = next(i for i in range(w) if pr[i] != row[i])
            i1 = next(i for i in range(w - 1, -1, -1) if pr[i] != row[i])
        if y == h - 1:
            i1 = min(i1, w - 2)                # never write the bottom-right cell: it would scroll
        if i0 > i1:
            continue
        changed = True
        out.append(f'\x1b[{y + 1};{i0 + 1}H')
        for x in range(i0, i1 + 1):
            ch, f, b, l = row[x]
            tgt = link_target(sess, l)
            if tgt != cur_link:
                out.append(f'\x1b]8;;{tgt or ""}\x1b\\')
                cur_link = tgt
            a = attr_of(sess, f & 15, b, bool(f & BLINK))
            if a != sess.attr:
                out.append(sgr(sess, a))
                sess.attr = a
            out.append(ch)
        if cur_link:
            out.append('\x1b]8;;\x1b\\')
            cur_link = None
    if cursor:
        out.append(f'\x1b[{cursor[1] + 1};{cursor[0] + 1}H\x1b[?25h')
    sess.prev = rows
    if not changed and len(out) <= 2 and not cursor:
        return ''
    return ''.join(out)
