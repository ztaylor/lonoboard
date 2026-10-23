"""What's on the board: the sections, the screens built from them, the footer and status line,
and the windows (details, help, display, chat, login, quit)."""
import re
import time
from datetime import datetime

from lonoboard import control
from lonoboard.board.logo import G, draw_logo, logo
from lonoboard.board.model import busy_of, chat_capable, chat_targets, expected, healthy, requests_today, status
from lonoboard.board.render import (BLINK, SPIN, Screen, boxed, center_segs, clean, dur, fade_rule, fit, frame_box, gib,
                            hdr, home, leader, pack, padl, padr, seg_len, strip_scheme, wrap)

WINDOW_S = 900            # telemetry window: 15 minutes
LANE_AX = 12              # graphs and activity lanes share a time axis starting here


class View:
    """What a session's renderers need: the hub's data, history, colours and the session's state."""

    def __init__(self, sess):
        self.s = sess
        self.hub = sess.hub
        self.local = sess.local_hub
        self.d = sess.hub.data
        self.hist = sess.hub.hist
        self.now = time.time()
        self.color = sess.hub.color
        self.utf8 = sess.utf8 or sess.kind == 'web'

    @property
    def name(self):
        return self.d['host']['board']['name']


# ---------- sections ----------

def sec_header(v, W, compact=False):
    d, h = v.d, v.d['host']
    name = h['board']['name']
    s = Screen(W)
    dot = ('  ∙  ', 9)
    labels = [[(h['board']['tagline'], 7)]] if h['board'].get('tagline') else []
    labels += [[(h['platform'], 7)], [(h['memory_label'], 7)], [(h['board']['exposure'], 7)]]
    px, dropped = None, False
    if not compact:
        m = re.match(r'^(.*?)([-_.]?)(\d+)$', name)
        full = [(m.group(1), 'A'), (m.group(2), 'D'), (m.group(3), 'B')] if m else [(name, 'A')]
        tries = [(full, 2), (full, 1)] + ([([(m.group(1), 'A')], 2), ([(m.group(1), 'A')], 1)] if m and m.group(1) else [])
        if name and all(ch in G for text, _ in full for ch in text.upper()):
            for parts, gap in tries:
                p = logo(parts, gap)
                if p[0] <= W:
                    px, dropped = p, parts is not full
                    break
    if not px:                                      # short window or a long name: a one-line banner
        center_segs(s, 0, [('░▒▓', 1), (f" {name} ", 15, 1), ('▓▒░', 1)])
        center_segs(s, 1, pack(labels, W - 2, dot)[0])
        fade_rule(s, 2, 0, W)
        return s
    x0 = (W - px[0]) // 2
    draw_logo(s, x0, 1, px)
    s.put(x0 - 4, 1, '·', 8)
    s.put(x0 - 2, 4, '∙', 1)
    s.put(x0 + px[0] + 2, 2, '∙', 8)
    s.put(x0 + px[0] + 4, 5, '·', 1)
    if dropped:
        s.segs(x0 + px[0] - len(name) - 6, 7, [('─═ ', 4), (name, 12), (' ═─', 4)])
    else:
        s.put(x0 + px[0] - len('lonoboard'), 7, 'lonoboard', 8)
    n = len(d['services'])
    items2 = [[('user ', 8), (h.get('user') or '-', 11)], [(str(n), 15), (' model server' + ('' if n == 1 else 's'), 8)],
              [(str(requests_today(d)), 15), (' requests today', 8)]]
    if d.get('fleet'):
        items2.append([(str(len(d['fleet']) + 1), 15), (' machines in the fleet', 8)])
    lines = [pack(labels, W, dot)[0], pack(items2, W, dot)[0]] if W >= 76 else pack(labels + items2, W - 2, dot)
    y = 8
    for ln in lines:
        center_segs(s, y, ln)
        y += 1
    fade_rule(s, y, 0, W)
    return s


def mem_mode(h):
    g = h.get('gpu') or {}
    if g.get('mem_total_gib'):
        return 'discrete'
    return 'unified' if g.get('unified') else 'ram'


def sec_boxes(v, SW):
    d, h = v.d, v.d['host']
    g = h.get('gpu') or {}
    side = SW >= 78
    bw = (SW - 1) // 2 if side else SW
    iw, LW = bw - 4, 13
    kv = lambda label, *vals: leader(label, LW) + list(vals)
    exp, ok = len(expected(d)), len(healthy(d))
    domain = ((h.get('tailnet') or {}).get('dns_name') or '').split('.')[1:2]
    stats = [kv('host', (h['hostname'], 15), *([(' · ' + domain[0], 8)] if domain else [])),
             kv('hardware', (h['platform'], 7))]
    if g:
        util, temp = g.get('util_pct'), g.get('temp_c')
        bars = max(0, iw - LW - 6)
        filled = round((util or 0) / 100 * bars)
        stats.append(kv('gpu util', (padl(f'{util:.0f}%' if util is not None else '-', 4), 15), (' ',),
                        ('█' * filled, 15 if (util or 0) > 75 else 11), ('░' * (bars - filled), 8)))
        if temp is not None:
            stats.append(kv('gpu temp', (f"{temp:.0f}°c", 14 if temp > 80 else 15)))
        if g.get('power_w') is not None:
            stats.append(kv('power', (f"{g['power_w']:.1f} w", 15)))
    stats += [kv('load avg', (' '.join(f'{x:.2f}' for x in h['load']), 15), (f" /{h['cpus']}", 8)),
              kv('uptime', (dur(h['uptime_s']), 15)),
              kv('requests', (str(requests_today(d)), 14), (' today', 8)),
              kv('services up', (f'{ok}/{exp}', 10 if ok == exp else 12))]
    if h.get('software'):
        stats.append(kv('software', (', '.join(h['software']), 7)))

    mode = mem_mode(h)
    ram_total, ram_avail = h['memory_gib']['total'], h['memory_gib']['available']
    if mode == 'discrete':
        title, total, used = 'gpu memory', g['mem_total_gib'], g.get('mem_used_gib') or 0
        per = lambda s: s['memory_mib'].get('gpu')
        head = [(f'{used:.1f}', 15), (f' / {total:.1f} gib vram', 7), ('  ram ', 8), (f'{ram_total - ram_avail:.0f}/{ram_total:.0f}g', 7)]
    else:
        title, total, used = 'memory', ram_total, ram_total - ram_avail
        per = (lambda s: s['memory_mib'].get('gpu')) if mode == 'unified' else (lambda s: s['memory_mib'].get('process'))
        head = [(f'{used:.1f}', 15), (f' / {total:.1f} gib used', 7), ('  ',), (f'{ram_avail:.1f}', 12 if ram_avail / max(total, 1) < .1 else 10), (' free', 8)]
    svcs = sorted(({'name': s['name'], 'g': per(s) / 1024, 'f': v.color(s['name'])} for s in d['services'] if per(s)),
                  key=lambda x: -x['g'])
    other = max(0, used - sum(x['g'] for x in svcs))
    total = max(total, 0.1)
    bar, acc, cells = [], 0, 0
    for x in svcs:
        acc += x['g']
        to = min(iw, round(acc / total * iw))
        if to > cells:
            bar.append(('█' * (to - cells), x['f']))
        cells = max(cells, to)
    acc += other
    to = min(iw, round(acc / total * iw))
    if to > cells:
        bar.append(('▓' * (to - cells), 8))
    cells = max(cells, to)
    bar.append(('·' * max(0, iw - cells), 8))
    NW, SZ = 11, 7
    bw2 = max(4, iw - NW - SZ)
    mx = max([0.1] + [x['g'] for x in svcs] + [other])
    mem_row = lambda name, gg, f, ch='█': [(padr(name, NW), f), ((ch * max(1, round(gg / mx * bw2))).ljust(bw2), f), (padl(f'{gg:.1f}g', SZ), 7)]
    mem = [head, bar] + [mem_row(x['name'], x['g'], x['f']) for x in svcs] + [mem_row('other', other, 8, '▓')]
    hgt = max(len(stats), len(mem))
    a, b = boxed(bw, 'system stats', stats, hgt), boxed(bw, title, mem, hgt)
    s = Screen(SW)
    s.blit(a, 0, 0)
    s.blit(b, SW - bw if side else 0, 0 if side else a.h)
    return s


def model_text(sv):
    m = sv.get('model') or {}
    if sv['kind'] == 'ollama':
        loaded = [x['id'] for x in sv.get('models') or [] if x.get('loaded')]
        if loaded:
            return loaded[0] + (f' +{len(loaded) - 1}' if len(loaded) > 1 else '')
        return f"{m.get('installed_count') or 0} installed"
    if m.get('id') or m.get('alias'):
        return m.get('id') or m.get('alias')
    if sv['kind'] == 'custom':
        return home((sv.get('command') or ['', 'custom'])[1]).split('/')[-1]
    return '-'


def state_text(v, sv, st):
    n, b = (sv.get('model') or {}).get('slots') or 0, busy_of(sv, v.hist)
    k = st[0]
    if k == 'busy':
        return [(SPIN[v.s.tick % len(SPIN)] + ' ', 3), (f'generating {b}/{n}' if n else f'generating · {b}', 11)]
    if k == 'ok':
        if sv['kind'] == 'ollama':
            loaded = (sv.get('model') or {}).get('loaded_count') or 0
            return [(f'{loaded} loaded' if loaded else 'idle', 7)]
        return [('idle', 7)]
    if k == 'warn':
        return [(st[1], 14)]
    if k == 'starting':
        return [('starting' + '.' * (1 + v.s.tick % 3), 14)]
    if k == 'bad':
        return [(st[1], 12 | BLINK)]
    return [('stopped', 8)]


def speed_text(sv, w):
    lr = (sv.get('model') or {}).get('last_request') or {}
    if lr.get('gen_tps'):
        return (padl(f"{lr['gen_tps']:.1f}", w), 14)
    if lr.get('prompt_tps'):
        return (padl(f"{round(lr['prompt_tps'])} in", w), 6)
    return (padl('-', w), 8)


def sec_services(v, SW):
    d, s = v.d, Screen(SW)
    exp, ok = len(expected(d)), len(healthy(d))
    busy = sum(1 for x in d['services'] if busy_of(x, v.hist) > 0)
    hdr(s, 0, 'services', f"{ok}/{exp} up{f' · {busy} busy' if busy else ''}")
    full = SW >= 76
    cols = ([('#', 3), ('service', 10), ('model', 18), ('state', 17), ('tok/s', 9, 1), ('gpu mem', 7, 1), ('uptime', 7, 1)] if full
            else [('#', 3), ('service', 10), ('state', 17), ('tok/s', 9, 1)])
    x = 1
    for c in cols:
        s.put(x, 2, padl(c[0], c[1]) if len(c) > 2 else padr(c[0], c[1]), 3)
        s.put(x, 3, '─' * c[1], 1)
        x += c[1] + 1
    if not d['services']:
        s.put(1, 4, 'no model servers found. add one to the config, or start one: see lonoboard check.', 8)
    for i, sv in enumerate(d['services']):
        y, st = 4 + i, status(sv, v.hist)
        since = sv['run'].get('since')
        cells = {
            '#': [(f'{i + 1:02d}', 11)], 'service': [(sv['name'], v.color(sv['name']))], 'model': [(model_text(sv), 7)],
            'state': state_text(v, sv, st),
            'gpu mem': [(padl(gib(sv['memory_mib'].get('gpu')), 7), 7)],
            'uptime': [(padl(dur(v.now - since).replace(' ', '') if since else '-', 7), 7)],
        }
        x = 1
        for c in cols:
            if c[0] == 'tok/s':
                s.segs(x, y, [speed_text(sv, c[1])])
            else:
                s.segs(x, y, [(fit(t[0], c[1]),) + tuple(t[1:]) for t in cells[c[0]]])
            x += c[1] + 1
        s.link(y, 0, SW, f'k:svc:{i}')
        if i == v.s.sel and not v.s.modal:
            s.fill_bg(y, 0, SW, 1)                         # the lightbar
            s.put(0, y, '»', 15, 1, f'k:svc:{i}')
    y = 5 + max(1, len(d['services']))
    arrows = '↑↓' if v.utf8 else 'up/dn'
    s.segs(1, y, [('[', 8), (arrows, 15), (']', 8), (' select · ', 8), ('[', 8), ('enter', 15), (']', 8),
                  (' details · tok/s = last request', 8)])
    return s


def bucket(v, PW, fn):
    x0, dt = v.now - WINDOW_S, WINDOW_S / PW
    out = [None] * PW
    for p in v.hist:
        val = fn(p)
        if val is None:
            continue
        i = int((p['t'] - x0) / dt)
        if 0 <= i < PW:
            out[i] = max(out[i] or 0, val)
    return out


BAND = [1, 9, 9, 3, 11, 15]


def bars(s, x0, y0, H, vals, vmax, width=1, hot=None):
    """Vertical bars in half-block steps, coloured by height."""
    for i, val in enumerate(vals):
        if val is None:
            continue
        lvl = round(val / vmax * H * 2) if vmax else 0
        for r in range(H):
            fb = H - 1 - r
            band = 15 if hot == i else BAND[min(len(BAND) - 1, fb * len(BAND) // H)]
            ch = '█' if lvl >= (fb + 1) * 2 else '▄' if lvl == fb * 2 + 1 else None
            if ch:
                s.put(x0 + i * width, y0 + r, ch * (width - (1 if width > 1 else 0)), band)


def sec_usage(v, SW):
    d, s = v.d, Screen(SW)
    H, AX = getattr(v, 'graph_h', 6), LANE_AX
    PW = SW - AX - 1
    g = d['host'].get('gpu') or {}
    has_gpu = any(p.get('gpu') is not None for p in v.hist) or g.get('util_pct') is not None
    vals = bucket(v, PW, lambda p: p.get('gpu')) if has_gpu else [None] * PW
    pk = max([0] + [p.get('gpu') or 0 for p in v.hist])
    hdr(s, 0, 'gpu usage', f'15 min · peak {pk:.0f}%' if has_gpu else 'no gpu utilisation reported')
    for r in range(H):
        y = 2 + r
        mid = r in (0, H // 2)
        s.put(1, y, padl('100%' if r == 0 else ' 50%' if r == H // 2 else '', AX - 3), 8)
        s.put(AX - 1, y, '┤' if mid else '│', 8)
        if mid:
            for i in range(0, PW, 3):
                s.put(AX + i, y, '·', 8)
    for i, val in enumerate(vals):
        if val is not None and round(val / 100 * H * 2) <= 0:
            s.put(AX + i, 2 + H - 1, '_', 1)
    bars(s, AX, 2, H, vals, 100)
    ya = 2 + H
    s.put(1, ya, padl('0%', AX - 3), 8)
    s.put(AX - 1, ya, '└' + '─' * PW, 8)
    for f in (1 / 3, 2 / 3):
        s.put(AX + round(PW * f), ya, '┴', 8)
    s.put(AX, ya + 1, '-15m', 8)
    s.put(AX + round(PW / 3) - 2, ya + 1, '-10m', 8)
    s.put(AX + round(PW * 2 / 3) - 1, ya + 1, '-5m', 8)
    s.put(SW - 3, ya + 1, 'now', 7)
    peak = lambda k: max([0] + [p.get(k) or 0 for p in v.hist])
    mem = d['host']['memory_gib']
    items = []
    if g.get('power_w') is not None:
        items.append([('pwr ', 3), (f"{g['power_w']:.1f}w", 15), (f" pk {peak('power'):.1f}", 8)])
    if g.get('temp_c') is not None:
        items.append([('temp ', 3), (f"{g['temp_c']:.0f}°c", 15), (f" pk {peak('temp'):.0f}", 8)])
    if mem_mode(d['host']) == 'discrete':
        items.append([('vram ', 3), (f"{g.get('mem_used_gib') or 0:.1f}", 15), (f"/{g['mem_total_gib']:.0f}g", 8)])
    items.append([('ram ', 3), (f"{mem['total'] - mem['available']:.1f}", 15), (f"/{mem['total']:.0f}g", 8)])
    summary = pack(items, SW - 2, ('   ', 7))
    for j, ln in enumerate(summary):
        s.segs(1, ya + 3 + j, ln)
    s.blit(lanes(v, SW), 0, ya + 4 + len(summary))
    return s


def lanes(v, SW, title=False):
    """One row per service: when it was busy over the last 15 minutes."""
    d, s, AX = v.d, Screen(SW), LANE_AX
    PW = SW - AX - 1
    y = 0
    if title:
        hdr(s, 0, 'activity', 'last 15 min')
        y = 2
    s.segs(1, y, ([('activity', 3), (' · ', 8)] if not title else []) + [('■', 11), (' busy  ', 8), ('·', 8), (' idle', 8)])
    y += 1
    for sv in d['services']:
        s.put(1, y, padr(sv['name'], AX - 2), v.color(sv['name']))
        s.put(AX - 1, y, '│', 8)
        if not any(sv['name'] in p.get('busy', {}) for p in v.hist):
            s.put(AX, y, 'awaiting samples' if sv['kind'] == 'llama.cpp' else f"{sv['kind']} doesn't report when it's busy", 8)
        else:
            for i, b in enumerate(bucket(v, PW, lambda p: p.get('busy', {}).get(sv['name']))):
                s.put(AX + i, y, '■' if b else '·', v.color(sv['name']) if b else 8)
        y += 1
    return s


def sec_hourly(v, SW):
    s, u = Screen(SW), v.hub.usage
    total = sum(b['requests'] for b in u['hours']) if u else 0
    hdr(s, 0, 'requests per hour', f'last 24 h · {total} total')
    if not u:
        s.put(1, 2, 'counting...', 8)
        return s
    H, AX = getattr(v, 'hourly_h', 7), 6
    hours = u['hours'][-24:]
    bw = max(2, min(3, (SW - AX - 1) // len(hours)))
    vmax = max([1] + [b['requests'] for b in hours])
    for r in range(H):
        s.put(AX - 1, 2 + r, '│', 8)
    s.put(0, 2, padl(str(vmax), AX - 2), 8)
    s.put(0, 2 + H // 2, padl(str(vmax // 2) if vmax > 1 else '', AX - 2), 8)
    bars(s, AX, 2, H, [b['requests'] or None for b in hours], vmax, bw, hot=len(hours) - 1)
    s.put(0, 2 + H, padl('0', AX - 2), 8)
    s.put(AX - 1, 2 + H, '└' + '─' * (bw * len(hours)), 8)
    for i, b in enumerate(hours):
        if b['hour'] % 3 == 0 or i == len(hours) - 1:
            s.put(AX + i * bw, 3 + H, f"{b['hour']:02d}", 15 if i == len(hours) - 1 else 8)
    busiest = max(hours, key=lambda b: b['requests'])
    if busiest['requests']:
        s.segs(1, 5 + H, [('busiest hour ', 3), (f"{busiest['hour']:02d}:00", 15),
                          (f" · {busiest['requests']} requests · {busiest['tokens']:,} tokens", 8)])
    return s


def sec_top(v, SW):
    s, u = Screen(SW), v.hub.usage
    hdr(s, 0, 'top 10', 'tokens generated today')
    rows = (u or {}).get('top') or []
    if not rows:
        s.put(1, 2, 'no requests today.' if u else 'counting...', 8)
        return s
    cols = [('#', 3), ('service', 10), ('requests', 8, 1), ('tokens', 9, 1), ('avg tok/s', 9, 1)]
    x = 1
    for c in cols:
        s.put(x, 2, padl(c[0], c[1]) if len(c) > 2 else padr(c[0], c[1]), 3)
        s.put(x, 3, '─' * c[1], 1)
        x += c[1] + 1
    barw = max(0, SW - x - 1)
    mx = max(r['tokens'] for r in rows) or 1
    for i, r in enumerate(rows[:10]):
        y, f = 4 + i, v.color(r['name'])
        vals = [(f'{i + 1:02d}.', 15 if i == 0 else 11), (padr(r['name'], 10), f), (padl(r['requests'], 8), 7),
                (padl(f"{r['tokens']:,}", 9), 15), (padl(f"{r['avg_tps']:.1f}" if r['avg_tps'] else '-', 9), 14)]
        x = 1
        for (t, col), c in zip(vals, cols):
            s.put(x, y, t, col)
            x += c[1] + 1
        n = max(1, round(r['tokens'] / mx * barw)) if barw and r['tokens'] else 0
        s.put(x, y, '▓' * n, f)
    return s


def sec_requests(v, SW, n=10):
    d, s = v.d, Screen(SW)
    hdr(s, 0, 'recent requests', f'{requests_today(d)} today')
    full = SW >= 70
    idx = {sv['name']: i for i, sv in enumerate(d['services'])}
    cols = ([('date', 5), ('time', 5), ('#', 2), ('service', 10), ('tokens', 7, 1), ('tok/s', 10, 1), ('prompt', 18, 1)] if full
            else [('time', 5), ('service', 10), ('tokens', 7, 1), ('tok/s', 10, 1)])
    x = 1
    for c in cols:
        s.put(x, 2, padl(c[0], c[1]) if len(c) > 2 else padr(c[0], c[1]), 3)
        s.put(x, 3, '─' * c[1], 1)
        x += c[1] + 1
    lst = v.hub.callers[:n]
    if not lst:
        s.put(1, 4, 'no requests logged yet.', 8)
    for j, c in enumerate(lst):
        y, dt = 4 + j, datetime.fromtimestamp(c['at'])
        tok = c['tokens'] if c.get('tokens') is not None else c.get('prompt_tokens')
        if c.get('gen_tps'):
            speed = (padl(f"{c['gen_tps']:.1f}", 10), 14)
        elif c.get('prompt_tps'):
            speed = (padl(f"{round(c['prompt_tps'])} in", 10), 6)
        elif c.get('duration_s') is not None:
            speed = (padl(f"{c['duration_s']:.1f}s", 10), 7)
        else:
            speed = (padl('-', 10), 8)
        vals = {
            'date': (dt.strftime('%m/%d'), 8), 'time': (dt.strftime('%H:%M'), 15 if v.now - c['at'] < 60 else 7),
            '#': (f"{idx.get(c['name'], -1) + 1:02d}", 11), 'service': (c['name'], v.color(c['name'])),
            'tokens': (padl(f'{tok:,}' if tok is not None else '-', 7), 15), 'tok/s': speed,
            'prompt': (padl(f"{c['prompt_tokens']:,} @ {round(c['prompt_tps'])}/s"
                            if c.get('prompt_tokens') is not None and c.get('gen_tps') and c.get('prompt_tps') else '', 18), 8),
        }
        x = 1
        for col in cols:
            t, f = vals[col[0]]
            s.put(x, y, fit(t, col[1]), f)
            x += col[1] + 1
        if c['name'] in idx:
            s.link(y, 0, SW, f"k:svc:{idx[c['name']]}")
    return s


def sec_events(v, SW, n=None, newest_first=False):
    s = Screen(SW)
    ev = list(v.hub.events)
    meta = f"{len(ev)} event{'' if len(ev) == 1 else 's'}" + (' · newest first' if newest_first and len(ev) > 1 else '')
    hdr(s, 0, 'event log', meta)
    rows = ev[-n:] if n else ev
    for j, (t, segs) in enumerate(reversed(rows) if newest_first else rows):
        x = s.put(1, 2 + j, datetime.fromtimestamp(t).strftime('%H:%M:%S'), 8)
        s.segs(x + 2, 2 + j, [(fit(seg[0], SW - 12),) + tuple(seg[1:]) for seg in segs])
    return s


def sec_endpoints(v, SW):
    d, s = v.d, Screen(SW)
    hdr(s, 0, 'endpoints', d['host']['board']['exposure'])
    NW, y = 15, 2
    if not d['services']:
        s.put(1, y, 'no model servers yet.', 8)
        y += 1
    for sv in d['services']:
        url = (sv['urls']['remote'] or [sv['urls']['local']])[0]
        x = s.put(1, y, padr(sv['name'], NW - 2), v.color(sv['name'])) + 1
        x = s.put(x, y, strip_scheme(url), 15, 0, f'u:{url}' if url.startswith('http') else None)
        eps = ' '.join(e for e in sv['endpoints'] if e not in ('/health', '/v1/models'))
        if SW - x - 3 > 6:
            s.put(x + 2, y, fit(eps, SW - x - 3), 8)
        y += 1
    routes = [o for o in d['other'] if o['name'] not in ('lonoboard', 'lonoboard telnet')]
    if routes:
        y += 1
        s.put(1, y, 'also served on this machine', 3)
        y += 1
        for o in routes:
            url = (o['urls']['remote'] or [o['urls']['local']])[0]
            x = s.put(1, y, padr((o['name'] or o['process'] or f"port {o['port']}").lower(), NW - 2), 7) + 1
            x = s.put(x, y, fit(strip_scheme(url), SW - x - 18), 7 if o['listening'] else 8, 0,
                      f'u:{url}' if o['listening'] and url.startswith('http') else None)
            s.segs(x + 2, y, [('[', 8), ('live', 10), (']', 8)] if o['listening'] else [('[', 8), ('not listening', 12), (']', 8)])
            y += 1
    return s


def fleet_rows(v):
    """[(name, url, summary or None, is this the machine you're on)] - always from your own board."""
    local = v.local
    d = local.data or {}
    me = d.get('host', {}).get('board', {}).get('name') or 'this machine'
    rows = [(me, None, None, True)]
    for p in d.get('fleet', []):
        rows.append((p['name'], p['url'], local.fleet.get(p['name']), False))
    return rows


def sec_fleet(v, SW):
    s = Screen(SW)
    rows = fleet_rows(v)
    hdr(s, 0, 'fleet', 'machines running lonoboard')
    cols = [('#', 3), ('machine', 12), ('state', 12), ('up', 7, 1), ('busy', 5, 1), ('gpu', 5, 1), ('memory', 11, 1), ('today', 6, 1)]
    if SW >= 100:
        cols.append(('hardware', 24))
    x = 1
    for c in cols:
        s.put(x, 2, padl(c[0], c[1]) if len(c) > 2 else padr(c[0], c[1]), 3)
        s.put(x, 3, '─' * c[1], 1)
        x += c[1] + 1
    viewing = v.hub.label if v.hub.remote else None
    for i, (name, url, f, mine) in enumerate(rows):
        y = 4 + i
        if mine:
            d = v.local.data
            up, tot = len(healthy(d)), len(expected(d))
            g, m = d['host'].get('gpu') or {}, d['host']['memory_gib']
            f = {'ok': True, 'services_up': up, 'services_total': tot,
                 'busy': sum(1 for x in d['services'] if busy_of(x, v.local.hist) > 0), 'gpu_util': g.get('util_pct'),
                 'mem_used_gib': round(m['total'] - m['available'], 1), 'mem_total_gib': m['total'],
                 'requests_today': requests_today(d), 'platform': d['host']['platform']}
        state = ([('this machine', 14)] if mine else [('viewing', 11)] if name == viewing else
                 [('checking', 8)] if f is None else [('online', 10)] if f.get('ok') else [('unreachable', 12)])
        ok = bool(f and f.get('ok'))
        vals = {
            '#': [(f'{i + 1:02d}', 11)], 'machine': [(name, 15)], 'state': state,
            'up': [(padl(f"{f['services_up']}/{f['services_total']}" if ok else '-', 7), 10 if ok and f['services_up'] == f['services_total'] else 7)],
            'busy': [(padl(str(f['busy']) if ok else '-', 5), 11 if ok and f['busy'] else 7)],
            'gpu': [(padl(f"{f['gpu_util']:.0f}%" if ok and f.get('gpu_util') is not None else '-', 5), 7)],
            'memory': [(padl(f"{f['mem_used_gib']:.0f}/{f['mem_total_gib']:.0f}g" if ok else '-', 11), 7)],
            'today': [(padl(str(f['requests_today']) if ok else '-', 6), 7)],
            'hardware': [((f or {}).get('platform') or '', 8)],
        }
        x = 1
        for c in cols:
            s.segs(x, y, [(fit(t[0], c[1]),) + tuple(t[1:]) for t in vals[c[0]]])
            x += c[1] + 1
        s.link(y, 0, SW, f'k:fleet:{i}')
        if i == v.s.fleet_sel and v.s.screen == 'fleet' and not v.s.modal:
            s.fill_bg(y, 0, SW, 1)
            s.put(0, y, '»', 15, 1, f'k:fleet:{i}')
    y = 5 + len(rows)
    arrows = '↑↓' if v.utf8 else 'up/dn'
    if len(rows) == 1:
        s.put(1, y, 'no other machines yet: run lonoboard on them (same port), or list them under [fleet] peers.', 8)
    else:
        s.segs(1, y, [('[', 8), (arrows, 15), (']', 8), (' select · ', 8), ('[', 8), ('enter', 15), (']', 8),
                      (" view that machine's board here", 8)])
        if v.s.kind == 'telnet':
            s.put(1, y + 1, 'or connect to it directly: telnet ' + rows[1][0], 8)
    return s


SCREENS = [('main', 'm', 'main'), ('services', 's', 'services'), ('host', 'h', 'host'), ('usage', 'u', 'usage'),
           ('requests', 'r', 'requests'), ('events', 'e', 'events'), ('network', 'n', 'network'), ('fleet', 'f', 'fleet')]
MENU = [('s', 'services'), ('h', 'host'), ('u', 'usage'), ('r', 'requests'), ('e', 'events'), ('n', 'network'),
        ('f', 'fleet'), ('c', 'chat'), ('d', 'display')]


def menu_items(sess):
    return MENU + ([('q', 'quit')] if sess.kind == 'telnet' else [])


def sec_menu(v, SW):
    """The main menu: every entry with a live one-line summary of what's behind it."""
    d, s = v.d, Screen(SW)
    hdr(s, 0, 'main menu', '[letter] or ↑↓ + enter' if v.utf8 else '[letter] or up/dn + enter')
    g, mem = d['host'].get('gpu') or {}, d['host']['memory_gib']
    exp, ok = len(expected(d)), len(healthy(d))
    busy = sum(1 for x in d['services'] if busy_of(x, v.hist) > 0)
    last = v.hub.callers[0] if v.hub.callers else None
    ev = list(v.hub.events)
    chat = sorted({sv for sv, _ in chat_targets(d)})
    fleet = fleet_rows(v)
    up = sum(1 for _, _, f, mine in fleet if mine or (f and f.get('ok')))
    host_sum = ([(f"gpu {g['util_pct']:.0f}%", 15)] if g.get('util_pct') is not None else [(h_cpu(d), 15)])
    host_sum += [(f" · {g['temp_c']:.0f}°c" if g.get('temp_c') is not None else '', 7),
                 (f" · {mem['total'] - mem['available']:.0f}/{mem['total']:.0f}g", 7)]
    summary = {
        's': [(f'{ok}/{exp} up', 10 if ok == exp else 12), (f' · {busy} busy' if busy else ' · idle', 11 if busy else 7)],
        'h': host_sum,
        'u': [(str(requests_today(d)), 15), (' requests today', 7)],
        'r': [('last ', 7), (datetime.fromtimestamp(last['at']).strftime('%H:%M') + ' ', 15), (last['name'], v.color(last['name']))] if last else [('none yet', 8)],
        'e': [(str(len(ev)), 15), (f" since {datetime.fromtimestamp(v.hub.started).strftime('%H:%M')}", 7)],
        'n': [(f"{len(d['services'])} endpoints", 15), (f" · {d['host']['board']['exposure']}", 7)],
        'f': [(f"{up}/{len(fleet)}", 15), (' machine' + ('' if len(fleet) == 1 else 's') + ' online', 7)],
        'c': ([('talk to ', 7), (', '.join(chat), 15)] if chat else
              [('turned off', 8)] if not d['host']['board'].get('chat', True) else [('no chat models', 8)]),
        'd': [('font · palette · colours', 7)],
        'q': [('disconnect · ', 7), (f'{dur(v.now - v.s.started)} online', 8)],
    }
    two = SW >= 76
    colw = (SW - 1) // 2 if two else SW
    for i, (key, label) in enumerate(menu_items(v.s)):
        col, row = (i % 2, i // 2) if two else (0, i)
        x0, y = col * (colw + 1), 2 + row
        segs = [(' [', 8), (key.upper(), 15), ('] ', 8), (padr(label, 9), 11)]
        room = colw - seg_len(segs) - 1
        for t in summary[key]:
            if room <= 0:
                break
            segs.append((fit(t[0], room),) + tuple(t[1:]))
            room -= len(t[0])
        s.segs(x0, y, segs)
        s.link(y, x0, x0 + colw, f'k:key:{key}')
        if i == v.s.menu_sel and v.s.screen == 'main' and not v.s.modal:
            s.fill_bg(y, x0, x0 + colw, 1)
    return s


def h_cpu(d):
    return f"load {d['host']['load'][0]:.2f}"


def sec_detail_panel(v, SW):
    """Wide services screen: live details for the service under the lightbar."""
    d, s = v.d, Screen(SW)
    if not d['services']:
        return s
    sv = d['services'][min(v.s.sel, len(d['services']) - 1)]
    hdr(s, 0, f"details · {sv['name']}", '[enter] full window')
    lines, _ = svc_lines(v, SW + 2, sv['name'], journal=False)
    y = 2
    for ln in lines:
        if isinstance(ln, dict):
            y += 1
            x = s.put(1, y, f"{ln['divider']} ", 3)
            s.put(x, y, '─' * max(0, SW - x - 1), 8)
        else:
            s.segs(1, y, [(t[0], t[1] if len(t) > 1 else 7, 0) + tuple(t[3:]) for t in ln])
        y += 1
    return s


def build_screen(v, W, vh):
    s = v.s
    s.sel_row = None
    out = Screen(W)
    wide = W >= 160
    SW = (W - 2) // 2 if wide else W

    def stack(scrs, x, y0):
        y = y0
        for sc in scrs:
            out.blit(sc, x, y)
            y += sc.h + 1
        return y

    def panels(left, right, y0=0):
        if wide:
            stack([f(v, SW) for f in left], 0, y0)
            stack([f(v, SW) for f in right], W - SW, y0)
        else:
            stack([f(v, W) for f in left + right], 0, y0)

    scr = s.screen
    if scr == 'main':
        head = sec_header(v, W, compact=vh < 20)
        out.blit(head, 0, 0)
        y = head.h + 1
        # fill the window, not more: extra rows go to recent requests and events
        if wide:
            boxes, menu = sec_boxes(v, SW), sec_menu(v, SW)
            left = [boxes]
            n_req = vh - (y + boxes.h + 1) - 5
            if n_req >= 2 and v.hub.callers:
                left.append(sec_requests(v, SW, n=min(n_req, len(v.hub.callers))))
            n_ev = vh - (y + menu.h + 1) - 3
            stack(left, 0, y)
            stack([menu] + ([sec_events(v, SW, n_ev)] if n_ev >= 2 else []), W - SW, y)
        else:
            menu, boxes = sec_menu(v, W), sec_boxes(v, W)
            parts = [boxes, menu] if y + boxes.h + 1 + menu.h <= vh else [menu]
            n_ev = vh - (y + sum(p.h + 1 for p in parts)) - 3
            if n_ev >= 2:
                parts.append(sec_events(v, W, n_ev))
            stack(parts, 0, y)
    elif scr == 'services':
        if wide:                                   # live details beside the table; narrow: enter opens them
            panels([sec_services, lambda v, sw: lanes(v, sw, title=True)], [sec_detail_panel])
        else:
            stack([sec_services(v, W)], 0, 0)
        s.sel_row = 4 + s.sel
    elif scr == 'host':
        if wide:                                   # graphs grow to fill a tall window
            v.graph_h = max(6, min(18, vh - len(v.d['services']) - 14))
        panels([sec_boxes], [sec_usage])
    elif scr == 'usage':
        top_h = 6 + len((v.hub.usage or {}).get('top') or [])
        v.hourly_h = max(7, min(20, vh - 7 - (0 if wide else top_h)))
        panels([sec_hourly], [sec_top])
    elif scr == 'requests':
        stack([sec_requests(v, W, n=len(v.hub.callers))], 0, 0)
    elif scr == 'events':
        stack([sec_events(v, W, newest_first=True)], 0, 0)
    elif scr == 'network':
        stack([sec_endpoints(v, W)], 0, 0)
    elif scr == 'fleet':
        stack([sec_fleet(v, W)], 0, 0)
        s.sel_row = 4 + s.fleet_sel
    return out


def more_prompt(v, W, page, pages):
    s = Screen(W)
    last = page == pages - 1
    s.segs(0, 0, [(' -- end -- ' if last else ' -- more -- ', 0, 7), ('  [', 8), ('space', 15), (']', 8),
                  (' next screen' if last else ' next page', 7), ('  [', 8), ('b', 15), (']', 8),
                  (' back', 7), (f'   page {page + 1} of {pages}', 8)])
    s.link(0, 0, 12, 'k:key: ')
    return s


def build_footer(v, W, rows):
    """Screen bar + prompt, pinned above the status line. Returns (screen, cursor x, cursor y)."""
    s = Screen(W)
    y = 0
    if rows >= 36:
        fade_rule(s, 0, 0, W, '═')
        y = 1
    x = 0
    chatting = bool(v.s.modal) and v.s.modal[0] == 'chat'
    bar = [(key, label, name == v.s.screen and not chatting, False) for name, key, label in SCREENS]
    if chat_targets(v.d):                              # chat is a window, not a screen, but it's a main feature
        bar.append(('c', 'chat', chatting, True))
    def entry(key, label, cur, divided, tight):
        bg, pad = (1 if cur else 0), (' ' if cur or not tight else '')
        return ([(' ' if tight else '│', 8)] if divided else []) + \
            [(' ', 7, bg), (key.upper(), 14 if cur else 15, bg), (' ' + label + pad, 15 if cur else 7, bg)]
    tight = sum(seg_len(entry(*e, False)) for e in bar) > W      # squeeze rather than wrap onto a second row
    for key, label, cur, divided in bar:               # the screen bar: where you are, where you can go
        segs = entry(key, label, cur, divided, tight)
        if x + seg_len(segs) > W:
            x, y = 0, y + 1
            segs = segs[1:] if divided else segs
        x0 = x
        x = s.segs(x, y, segs)
        s.link(y, x0 + (1 if divided and x0 else 0), x, f'k:key:{key}')
    y += 1
    label = dict((n, l) for n, _, l in SCREENS)[v.s.screen]
    where = v.name if not v.hub.remote else f"{v.name} via {(v.local.data or {}).get('host', {}).get('board', {}).get('name', '')}"
    cx = s.segs(1, y, [('[', 8), (where, 13 if v.hub.remote else 11), (']', 8), (f' {label} : ', 7)])
    arrows, flips = ('↑↓', '←→') if v.utf8 else ('up/dn', '<>')
    scr = v.s.screen
    hints = ([('q', 'quit', 'q')] if v.s.kind == 'telnet' else []) + [(flips, 'screens', None)]
    if v.hub.remote:
        hints.append(('esc', f"back to {(v.local.data or {}).get('host', {}).get('board', {}).get('name', 'your board')}", 'esc'))
    if scr in ('services', 'fleet', 'main'):
        hints += [(arrows, 'select', None), ('enter', 'details' if scr == 'services' else 'view' if scr == 'fleet' else 'open', 'enter')]
    if v.s.pages > 1:
        hints += [('space/b', 'page', ' ')]
    hints += [('?', 'help', '?'), ('d', 'display', 'd')]
    prompt_y, x = y, cx + 3
    wrap_x = cx + 3 if W - cx >= 44 else 1             # line up under the first hint, unless that's too cramped
    for k, lab, act in hints:                          # every command shows: wrap onto another row, never drop
        segs = [('[', 8), (k, 15), (']', 8), (f' {lab}', 7)]
        if x > wrap_x and x + seg_len(segs) > W - 1:
            y, x = y + 1, wrap_x
        x0 = x
        x = s.segs(x, y, segs)
        if act:
            s.link(y, x0, x, f'k:key:{act}')
        x += 2
    s.row(y + 1)                       # status line goes here (drawn by the caller)
    return s, cx, prompt_y


def build_status(v, W):
    s, d = Screen(W), v.d
    bg = 5 if v.hub.remote else 3                     # magenta when you're looking at another machine
    s.fill_bg(0, 0, W, bg)
    t = int(v.now - v.s.started)
    sess = f'{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}'
    parts = [[(f" {v.name.upper()} ", 15 if v.hub.remote else 0, bg)]]
    if v.hub.remote:
        parts.append([(' REMOTE ' if not v.hub.error else ' NO LINK ', 15, bg)])
    parts.append([(f' {len(healthy(d))}/{len(expected(d))} UP ', 0, bg)])
    if W >= 72:
        b = sum(busy_of(x, v.hist) for x in d['services'])
        parts.append([(f' {b} BUSY ', 15 if b else 0, bg)])
    if W >= 64:
        parts.append([(f" {'WEB' if v.s.kind == 'web' else 'TELNET ' + ('UTF-8' if v.s.utf8 else 'CP437')} {v.s.cols}x{v.s.rows} ", 0, bg)])
    names = [n for n, _, _ in SCREENS]
    where = f'SCREEN {names.index(v.s.screen) + 1}/{len(names)}' + (f' PAGE {v.s.page + 1}/{v.s.pages}' if v.s.pages > 1 else '')
    clock = datetime.fromtimestamp(v.now).strftime('%H:%M:%S')
    right = [where, f'SESSION {sess}', clock]
    for drop in (f'SESSION {sess}', clock):   # the session timer goes first, then the clock; the position stays
        if len(right) > 1 and len(' │ '.join(right)) + 2 > W // 2:
            right.remove(drop)
    txt = ' ' + ' │ '.join(right) + ' '
    while len(parts) > 1 and sum(seg_len(p) + 1 for p in parts) > W - len(txt) - 1:
        parts.pop()
    x = 0
    for i, p in enumerate(parts):
        x = s.segs(x, 0, p)
        if i < len(parts) - 1:
            x = s.put(x, 0, '│', 1, bg)
    x = W - len(txt)
    for i, part in enumerate(right):
        x = s.put(x, 0, ' ' + part + ' ', 0, bg)
        if i < len(right) - 1:
            x = s.put(x, 0, '│', 1, bg)
    return s


# ---------- windows ----------

def build_modal(v, W, vh):
    kind = v.s.modal[0]
    if kind == 'chat':
        return build_chat(v, W, vh)
    if kind == 'login':
        return build_login(v, W)
    if kind == 'quit':
        B = lambda t, f=7, l=None: (t, f, 1, l)
        lines = [[B('disconnect from '), B(v.name, 15), B('?')], [],
                 [B('  '), B('[', 9, 'k:key:y'), B('Y', 15, 'k:key:y'), B('] yes', 9, 'k:key:y'), B('    '),
                  B('[', 9, 'k:key:n'), B('N', 15, 'k:key:n'), B('] no', 9, 'k:key:n')]]
        box, _ = frame_box(min(W, 44), 'quit', lines, '[enter] yes · [esc] no')
        return box, None
    if kind == 'confirm':
        return build_confirm(v, W), None
    if kind == 'help':
        lines, title, mw = help_lines(v), 'help', 70
    elif kind == 'display':
        lines, title, mw = display_lines(v), 'display', 78
    else:
        res = svc_lines(v, min(W, 78))
        if res is None:
            v.s.modal = None
            return None, None
        lines, title = res
        mw = 78
    mw = min(W, mw)
    foot = '[esc] close' + (' · [↑↓] other services' if kind == 'svc' and v.utf8 else ' · [up/dn] other services' if kind == 'svc' else '')
    height = min(len(lines) + 2, vh - 1)
    box, v.s.modal_scroll = frame_box(mw, title, lines, foot, height, v.s.modal_scroll)
    return box, None


def build_confirm(v, W):
    """Start qwen? [Y/n] - and, when its weights won't fit in free memory, start it anyway?"""
    _, name, action, tight = v.s.modal
    B = lambda t, f=7, l=None: (t, f, 1, l)
    MW = min(W, 60)
    lines = [[B(ln, 15)] for ln in wrap(control.describe(action, name) + '?', MW - 4)]
    if tight:
        lines.append([])
        lines += [[B(ln, 14)] for ln in wrap(f"its weights need about {tight['need_gib']} gib and "
                                             f"{tight['free_gib']} gib is free.", MW - 4)]
        lines += [[B(ln)] for ln in wrap('it may fail to load, or push other things out of memory.', MW - 4)]
    lines += [[], [B('  '), B('[', 9, 'k:key:y'), B('Y', 15, 'k:key:y'), B('] yes', 9, 'k:key:y'), B('    '),
                   B('[', 9, 'k:key:n'), B('N', 15, 'k:key:n'), B('] no', 9, 'k:key:n')]]
    box, _ = frame_box(MW, 'start anyway' if tight else 'confirm', lines, '[enter] yes · [esc] no')
    return box


def control_segs(v, sv, width):
    """The details window's control row: [s] stop  [r] restart  [e] don't start at boot (or how it went)."""
    n = v.s.current_notice(sv['name'])
    if n:
        return [(fit(n['text'], width), n['color'], 1)]
    acts = control.actions_for(sv)
    long_ = [('s', 'stop' if 'stop' in acts else 'start')] + ([('r', 'restart')] if 'restart' in acts else []) + \
            [('e', "don't start at boot" if 'disable' in acts else 'start at boot')]
    short = [(k, label.replace("don't start at boot", 'boot off').replace('start at boot', 'boot on')) for k, label in long_]
    for opts in (long_, short, [(k, '') for k, _ in long_]):
        segs = []
        for key, label in opts:
            link = f'k:key:{key}'
            segs += [('  ' if segs else '', 7, 1), ('[', 9, 1, link), (key, 15, 1, link),
                     (']' + (f' {label}' if label else ''), 9, 1, link)]
        if sum(len(x[0]) for x in segs) <= width:
            return segs
    return segs


def build_login(v, W):
    s = v.s
    B = lambda t, f=7: (t, f, 1)
    mask = '*' * len(s.input)
    lines = [[B('this board is password protected.')], [],
             [B('password: ', 11), B(mask[-24:], 15)], []]
    if s.login_error:
        lines.append([B(s.login_error, 12)])
    box, _ = frame_box(min(W, 46), f'{v.name} · login', lines, '[enter] log in' + (' · [q] quit' if s.kind == 'telnet' else ''))
    return box, (2 + len('password: ') + min(len(mask), 24), 3)


def svc_lines(v, MW, name=None, journal=True):
    d, s = v.d, v.s
    names = [x['name'] for x in d['services']]
    name = name or s.modal[1]
    if name not in names:
        return None
    i = names.index(name)
    sv = d['services'][i]
    m, run, st = sv.get('model') or {}, sv.get('run') or {}, status(sv, v.hist)
    IW, LW = MW - 4, 13
    VW = IW - LW
    lines = []
    lab = lambda label: [(t, 9 if f == 8 else 11, 1) for t, f in leader(label, LW)]

    def kv(label, segs):
        plain = ''.join(x[0] for x in segs)
        if len(plain) <= VW:
            lines.append(lab(label) + segs)
        else:
            for j, ln in enumerate(wrap(plain, VW)):
                lines.append(([(' ' * LW, 7, 1)] if j else lab(label)) + [(ln, 15, 1)])

    B = lambda t, f=15: (t, f, 1)
    st_text = {'busy': 'running · generating', 'ok': 'running · idle', 'starting': 'starting', 'off': 'stopped'}.get(st[0], st[1])
    kv('status', [B(st_text, {'busy': 11, 'ok': 10, 'warn': 14, 'starting': 14, 'bad': 12, 'off': 7}[st[0]])])
    if s.may_control(sv):
        lines.append(lab('control') + control_segs(v, sv, VW))
    kv('server', [B(sv['kind']), B(f" · {sv['version']}" if sv.get('version') else '', 7), B(f" · found by {sv.get('origin', 'config')}", 7)])
    if m.get('id') and sv['kind'] != 'ollama':
        extra = ' · '.join(x for x in (f"{m['params_b']}b" if m.get('params_b') else '', (m.get('quant') or '').lower(),
                                       f"{m['size_gib']} gib" if m.get('size_gib') else '') if x)
        kv('model', [B(m['id']), B(f' · {extra}' if extra else '', 7)])
    if m.get('context'):
        kv('context', [B(f"{m['context']:,}"), B(f" x {m['slots']} slots" if m.get('slots') else '', 7),
                       B(f" · trained {m['context_train']:,}" if m.get('context_train') else '', 7)])
    if m.get('input_modalities'):
        kv('inputs', [B(', '.join(m['input_modalities']))])
    lr = m.get('last_request')
    if lr:
        if lr.get('tokens') is not None and lr.get('gen_tps'):
            main = f"{lr['tokens']:,} tok @ {lr['gen_tps']:.1f} t/s"
        elif lr.get('duration_s') is not None:
            main = f"{lr['duration_s']:.1f}s"
        else:
            main = f"{lr.get('prompt_tokens') or 0:,} tok in"
        extra = f" · prompt {lr['prompt_tokens']:,} @ {round(lr['prompt_tps'])}/s" if lr.get('prompt_tps') and lr.get('prompt_tokens') is not None and lr.get('gen_tps') else ''
        kv('last request', [B(main, 14), B(extra, 7), B(f" · {dur(v.now - lr['at'])} ago", 7)])
    if m.get('requests_today') is not None:
        kv('requests', [B(str(m['requests_today']), 14), B(' today', 7)])
    kv('memory', [B(gib(sv['memory_mib'].get('gpu'))), B(' gpu · ', 7), B(gib(sv['memory_mib'].get('process'))), B(' process', 7)])
    mgr = run.get('manager')
    if mgr == 'systemd':
        kv('unit', [B(run.get('unit') or '-'), B(f" · pid {run.get('pid') or '-'} · {run.get('restarts', 0)} restart{'' if run.get('restarts') == 1 else 's'}", 7)])
    elif mgr == 'docker':
        kv('container', [B(run.get('unit') or '-'), B(f" · {run.get('image') or ''} · {run.get('restarts', 0)} restarts", 7)])
    elif mgr == 'process':
        kv('process', [B(f"pid {run.get('pid')}")])
    if run.get('since'):
        kv('up since', [B(datetime.fromtimestamp(run['since']).strftime('%m/%d %H:%M')), B(f" ({dur(v.now - run['since'])})", 7)])
    if m.get('file'):
        kv('weights', [B(home(m['file']), 7)])
    if chat_capable(sv):
        lines.append([('press ', 7, 1), ('[c]', 15, 1), (' to chat with this model', 7, 1)])
    if sv['kind'] == 'ollama' and sv.get('models'):
        lines.append({'divider': f"models · {sum(1 for x in sv['models'] if x.get('loaded'))} loaded"})
        for x in sv['models']:
            info = ' · '.join(t for t in (f"{x['params_b']}b" if x.get('params_b') else '', (x.get('quant') or '').lower(),
                                          f"{x['size_gib']} gib" if x.get('size_gib') else '') if t)
            lines.append([('● ' if x.get('loaded') else '  ', 10 if x.get('loaded') else 8, 1), (fit(x['id'], 28).ljust(28), 15 if x.get('loaded') else 7, 1),
                          (fit(info, IW - 31), 7, 1)])
    lines.append({'divider': 'brief'})
    for ln in wrap(sv.get('description') or 'no description', IW):
        lines.append([(ln, 7, 1)])
    lines.append({'divider': 'endpoints'})
    base = (sv['urls']['remote'] or [sv['urls']['local']])[0]
    for e in sv['endpoints']:
        lines.append([(fit(strip_scheme(base), IW - len(e)), 15, 1, f'u:{base + e}'), (e, 11, 1, f'u:{base + e}')])
    args = m.get('args') or sv.get('command') or []
    if args:
        lines.append({'divider': 'command' if sv.get('command') else 'launch flags'})
        pairs, j = [], 0
        while j < len(args):
            a, nx = args[j], args[j + 1] if j + 1 < len(args) else None
            if a.startswith('-') and nx is not None and not nx.startswith('-'):
                pairs.append((a, home(nx)))
                j += 2
            else:
                pairs.append((home(a), ''))
                j += 1
        cur, n = [], 0
        for f, val in pairs:
            w = len(f) + (1 + len(val) if val else 0)
            if n and n + 2 + w > IW:
                lines.append(cur)
                cur, n = [], 0
            if n:
                cur.append(('  ', 7, 1))
                n += 2
            cur.append((fit(f, IW), 11, 1))
            if val:
                cur.append((fit(' ' + val, max(1, IW - n - len(f))), 15, 1))
            n += min(w, IW)
        if cur:
            lines.append(cur)
    if not journal:
        return lines, f'{i + 1:02d} · {name}'
    if v.now - s.journal_at > 4 or s.journal_for != (id(v.hub), name):
        s.journal, s.journal_at, s.journal_for = v.hub.logs(name, 30), v.now, (id(v.hub), name)
    lines.append({'divider': 'logs · last 30 lines'})
    for l in s.journal or ['(no logs available for this service)']:
        mm = re.match(r'^(\S+) (.*)$', l) or None
        t = re.sub(r'[+-]\d\d:?\d\d$|\.\d+Z?$', '', re.sub(r'^\d{4}-\d\d-\d\dT', '', mm.group(1))) if mm else ''
        if mm and not re.match(r'\d', t):
            t, msg = '', l
        else:
            msg = mm.group(2) if mm else l
        msg = re.sub(r'\s{2,}', ' ', re.sub(r'^\d+\.\d+\.\d+\.\d+ ', '', msg))
        head = msg[:40]
        f = 12 if re.search(r'^E |\b(ERROR|error|CRITICAL)\b', head) else 14 if re.search(r'^W |\b(WARN|WARNING)\b', head) else 7
        lines.append([(t + ' ' if t else '', 9, 1), (fit(clean(msg), IW - len(t) - 1), f, 1)])
    return lines, f'{i + 1:02d} · {name}'


def help_lines(v):
    B = lambda t, f=7: (t, f, 1)
    key = lambda k, what: [B('  '), B('[', 9), B(k, 15), B(']', 9), B(' ' * max(1, 8 - len(k))), B(what)]
    arrows = '↑↓' if v.utf8 else 'up/dn'
    lines = [
        [B('lonoboard', 14), B(' · a live board for the machines running your models.')],
        [],
        key('m s h u', 'screens: main · services · host · usage'),
        key('r e n f', 'screens: requests · events · network · fleet'),
        key('←→' if v.utf8 else '< >', 'previous / next screen'), key('esc', 'back to main (or to your own board)'),
        key(arrows, 'move the selection (main menu, services, fleet)'), key('enter', 'open it'), key('1-9', 'details for a service'),
        key('space', 'next page, then the next screen'), key('b', 'back a page'),
        key('c', 'chat with a model'), key('d', 'display options'),
    ]
    if v.s.may_control():
        lines += [key('s r e', "in a service's details: start or stop · restart · start at boot")]
    lines += [key('q', 'quit (g works too, as on the old boards)')] if v.s.kind == 'telnet' else []
    lines += [{'divider': 'notes'},
              [B('tok/s', 11), B('    generation speed of the last request')],
              [B('in', 11), B('       prompt tok/s (embeddings, reranking: no generation)')],
              [B('slots', 11), B('    parallel requests a llama.cpp server can serve')],
              [B('fleet', 11), B("    other machines running lonoboard; enter views their board here")]]
    if v.s.kind == 'web':
        lines += [[B('telnet', 11), B('   the same board is on telnet, if this machine publishes it')]]
    lines += [{'divider': 'credits'}, [B('font  ', 11), B('ibm vga 8x16 & 9x16, the ultimate oldschool')],
              [B('      '), B('pc font pack by vileR · int10h.org · cc by-sa 4.0')]]
    return lines


def display_lines(v):
    p, s = v.s.prefs, v.s
    B = lambda t, f=7: (t, f, 1)
    on = lambda b: B('on', 10) if b else B('off', 8)
    row = lambda k, label, val, note='': [B('  '), B('[', 9), B(k, 15), B(']', 9), B(f' {label:<14}'), val, B(f'  {note}', 8)]
    pal = {'vga': 'vga color', 'amber': 'amber mono', 'green': 'green mono'}[p['pal']]
    lines = [[B('press a key to change it. saved per browser.' if s.kind == 'web' else 'press a key to change it for this session.')], []]
    if s.kind == 'web':
        lines.append(row('f', 'font', B('ibm vga ' + p['font'], 15), '9x16 is the real 720x400 text mode'))
    lines.append(row('p', 'palette', B(pal, 15), 'vga · amber · green'))
    lines.append(row('i', 'bright bg', B('ice colors', 15) if p['ice'] else B('blink', 15), 'how high-intensity backgrounds show'))
    if s.kind == 'web':
        lines.append(row('s', 'crt effect', on(p['crt']), 'phosphor glow + scanlines'))
    else:
        if s.utf8:
            names = {'exact': '24-bit vga', '256': '256-colour vga', 'theme': 'terminal theme'}
            lines.append(row('m', 'colours', B(names[s.colors], 15), 'exact vga · 256 · your theme'))
        lines.append(row('u', 'characters', B('utf-8' if s.utf8 else 'cp437', 15), 'switch if boxes look wrong'))
    return lines


def build_chat(v, W, vh):
    s = v.s
    chat = s.current_chat()
    MW = min(W, 100)
    H = max(10, vh - 1)
    IW = MW - 4
    targets = chat_targets(v.d)
    lines = []
    if not chat.msgs:
        where = 'this machine' if not v.hub.remote else v.hub.label
        lines += [[(f'chatting with {chat.name}. type a message and press enter.', 7, 1)],
                  [(f'replies stream from the model on {where}.', 8, 1)]]
    col = v.color(chat.service)
    tag = chat.model or chat.service
    for m in chat.msgs:
        if m['role'] == 'user':
            lines.append([])
            for j, ln in enumerate(wrap(m['content'], IW - 5)):
                lines.append([('you  ' if j == 0 else '     ', 11, 1), (ln, 15, 1)])
            continue
        lines.append([])
        if m['reasoning']:
            if m['content'] or not chat.busy or m is not chat.msgs[-1]:
                lines.append([(tag + ' ', col, 1), (f"(thought for {m['think_s'] or 0:.0f}s · {len(m['reasoning'].split())} words)", 9, 1)])
            else:
                lines.append([(tag + ' ', col, 1), (f"thinking {SPIN[s.tick % len(SPIN)]}", 9, 1)])
                for ln in wrap(m['reasoning'], IW - 2)[-3:]:
                    lines.append([('  ' + ln, 9, 1)])
        body = wrap(m['content'], IW) if m['content'] else []
        if body:
            if not m['reasoning']:
                lines.append([(tag, col, 1)])
            for ln in body:
                lines.append([(ln, 7, 1)])
        elif not m['reasoning'] and chat.busy and m is chat.msgs[-1]:
            lines.append([(tag + ' ', col, 1), (SPIN[s.tick % len(SPIN)], 3, 1)])
        if m['error']:
            lines.append([(f"[{m['error']}]", 12, 1)])
        elif m['stats'] and m['stats'][0] and not (chat.busy and m is chat.msgs[-1]):
            lines.append([(f"{m['stats'][0]:,} tokens" + (f" · {m['stats'][1]:.1f} tok/s" if m['stats'][1] else ''), 9, 1)])
    body_h = H - 4
    chat.scroll = max(0, min(chat.scroll, max(0, len(lines) - body_h)))
    start = max(0, len(lines) - body_h - chat.scroll)
    shown = lines[start:start + body_h]
    others = f' · [tab] model ({len(targets)})' if len(targets) > 1 else ''
    foot = ('[esc] stop' if chat.busy else '[esc] close') + others + (' · [pgup] history' if start > 0 else '')
    box = Screen(MW)
    x = box.segs(0, 0, [('╔═╡', 15, 1), (f' chat · {chat.name} ', 14, 1), ('╞', 15, 1)])
    box.put(x, 0, '═' * max(0, MW - x - 1) + '╗', 15, 1)
    for y in range(1, H - 3):
        box.put(0, y, '║ ', 15, 1)
        box.fill_bg(y, 1, MW - 1, 1)
        ln = shown[y - 1] if y - 1 < len(shown) else []
        box.segs(2, y, ln)
        box.put(MW - 2, y, ' ║', 15, 1)
    box.put(0, H - 3, '╟' + '─' * (MW - 2) + '╢', 15, 1)
    box.put(0, H - 2, '║ ', 15, 1)
    box.fill_bg(H - 2, 1, MW - 1, 1)
    prompt = '> '
    visible = s.input[-(IW - len(prompt) - 1):]
    cx = box.segs(2, H - 2, [(prompt, 11, 1), (visible, 15, 1)])
    if chat.busy:
        box.put(MW - 14, H - 2, 'generating...', 8, 1)
    box.put(MW - 2, H - 2, ' ║', 15, 1)
    x = box.segs(0, H - 1, [('╚═╡', 15, 1), (f' {foot} ', 7, 1), ('╞', 15, 1)])
    box.put(x, H - 1, '═' * max(0, MW - x - 1) + '╝', 15, 1)
    return box, (cx, H - 2)
