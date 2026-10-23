"""Sessions: one per connected viewer (telnet or web), plus the Board that owns them.

A session renders the board onto a character grid sized to its client, diffs frames and sends
only the changed cells. It reads data from a hub - this machine's, or another machine's when
you're viewing it from the fleet screen.
"""
import hmac
import json
import re
import socket
import threading
import time
import uuid
from collections import deque

from lonoboard import control
from lonoboard.board.hub import Chat, Hub, RemoteHub
from lonoboard.board.model import chat_capable, chat_targets
from lonoboard.board.render import Screen, center_segs, clean, dur, encode
from lonoboard.config import peer_url
from lonoboard.board.screens import (SCREENS, View, build_footer, build_modal, build_screen, build_status, fleet_rows,
                             menu_items, more_prompt)

KEYSEQ = {'[A': 'up', '[B': 'down', '[C': 'right', '[D': 'left', 'OA': 'up', 'OB': 'down', 'OC': 'right',
          'OD': 'left', '[5~': 'pgup', '[6~': 'pgdn', '[H': 'home', '[F': 'end', '[1~': 'home', '[4~': 'end',
          '[7~': 'home', '[8~': 'end', 'OH': 'home', 'OF': 'end', '[3~': 'del', '[Z': 'btab'}
LOGIN_TRIES = 3
CONTROL_KEYS = ('s', 'r', 'e')           # in a service's details: start/stop, restart, start at boot or not
NOTICE_FOR = 10                          # seconds a control result stays on screen


def parse_keys(s):
    keys, i = [], 0
    while i < len(s):
        c = s[i]
        if c == '\x1b':
            if s.startswith('\x1bP', i):                      # private action from the web terminal
                j = s.find('\x1b\\', i)
                if j < 0:
                    break
                body, i = s[i + 2:j], j + 2
                if body.startswith('act:'):
                    keys.append(('act', body[4:]))
                elif body.startswith('wheel:'):
                    try:
                        keys.append(('wheel', int(body[6:])))
                    except ValueError:
                        pass
                continue
            m = re.match(r'\x1b\[<(\d+);(\d+);(\d+)([Mm])', s[i:])      # xterm SGR mouse report
            if m:
                keys.append(('mouse', (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) == 'M')))
                i += len(m.group(0))
                continue
            m = re.match(r'\x1b(\[[0-9;]*[A-Za-z~]|O[A-Z])', s[i:])
            if m:
                k = KEYSEQ.get(m.group(1))
                if k:
                    keys.append(k)
                i += len(m.group(0))
                continue
            keys.append('esc')
            i += 1
        elif c in '\r\n':
            keys.append('enter')
            i += 1
            if c == '\r' and i < len(s) and s[i] in '\n\0':
                i += 1
        elif c in '\x7f\x08':
            keys.append('bs')
            i += 1
        elif c == '\t':
            keys.append('tab')
            i += 1
        elif c == '\x03':
            keys.append('ctrlc')
            i += 1
        elif ord(c) < 32:
            i += 1
        else:
            keys.append(c)
            i += 1
    return keys


def clamp_size(cols, rows):
    """A window size a client claims -> one the board will draw (telnet clients can say 0 or 65535)."""
    return max(40, min(int(cols or 80), 320)), max(12, min(int(rows or 24), 200))


PREFS = {'font': ('8x16', '9x16'), 'pal': ('vga', 'amber', 'green')}


def clean_prefs(prefs):
    """Display settings from a browser, keeping only known keys and values."""
    out = {}
    for k, v in (prefs if isinstance(prefs, dict) else {}).items():
        if k in PREFS and v in PREFS[k]:
            out[k] = v
        elif k in ('ice', 'crt') and isinstance(v, bool):
            out[k] = v
    return out


class Board:
    """This machine's board: its hub, remote hubs for the fleet, the sessions and the settings."""

    def __init__(self, collector, cfg):
        b = cfg['board']
        self.cfg = cfg
        self.password = b['password'] or ''
        self.auth = {'Authorization': f'Bearer {self.password}'} if self.password else {}
        self.hub = Hub(collector)
        self.hub.auth = self.auth
        self.hub.ensure_running()
        self.max_sessions = b['max_sessions']
        self.max_per_ip = b.get('max_per_ip') or 0
        self.idle_timeout = (b.get('idle_timeout') or 0) * 60
        self.intro = b['intro']
        self.remotes = {}
        self.sessions = set()
        self.web = {}
        self.lock = threading.Lock()

    def full(self, ip=None):
        if len(self.sessions) >= self.max_sessions:
            return True
        if ip and self.max_per_ip:
            return sum(1 for s in self.sessions | set(self.web.values()) if s.alive and s.ip == ip) >= self.max_per_ip
        return False

    def trusted(self, url):
        """Peers listed in [fleet] peers get this board's password; ones found on the tailnet don't."""
        port = self.cfg['board']['port']
        return url.rstrip('/') in {peer_url(p, port) for p in self.cfg['fleet']['peers']}

    def remote(self, name, url):
        with self.lock:
            hub = self.remotes.get(url)
            if hub is None:
                hub = self.remotes[url] = RemoteHub(url, name)
                hub.auth = self.auth if self.trusted(url) else {}
            return hub

    def check_password(self, attempt):
        return bool(self.password) and hmac.compare_digest(attempt.encode(), self.password.encode())

    # --- web terminal sessions ---
    def web_create(self, cols, rows, prefs, intro, ip=None, login=None):
        for sid, s in list(self.web.items()):          # sweep sessions whose stream never attached or died
            if not s.alive or (not getattr(s, 'attached', False) and time.time() - s.last_seen > 30):
                s.alive = False
                self.web.pop(sid, None)
        if self.full(ip):
            return None
        s = Session(self, 'web', cols, rows, utf8=True, prefs=clean_prefs(prefs), intro=intro and self.intro)
        s.ip, s.login = ip, login
        s.resize(cols, rows)
        self.web[s.id] = s
        return s

    def web_stream(self, sess, handler):
        """Run a web session's render loop, writing ANSI as server-sent events."""
        sess.attached = True

        def write(text):
            handler.wfile.write(b'data: ' + json.dumps(text).encode() + b'\n\n')
            handler.wfile.flush()

        try:
            write('')
            sess.loop(write)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sess.alive = False
            self.web.pop(sess.id, None)


class Session:
    def __init__(self, board, kind, cols=80, rows=24, utf8=True, prefs=None, intro=True):
        self.board = board
        self.local_hub = self.hub = board.hub
        self.kind, self.utf8 = kind, utf8
        self.cols, self.rows = clamp_size(cols, rows)
        self.prefs = {'font': '8x16', 'pal': 'vga', 'ice': True, 'crt': True}
        self.prefs.update(prefs or {})
        self.intro = intro
        self.screen, self.page, self.pages, self.sel, self.menu_sel, self.fleet_sel = 'main', 0, 1, 0, 0, 0
        self.sel_row, self.last_wheel = None, 0
        self.modal, self.modal_scroll = None, 0
        self.chats, self.chat_key, self.input = {}, None, ''
        self.locked, self.login_tries, self.login_error = bool(board.password), 0, None
        self.dirty = threading.Event()
        self.inbox = deque()
        self.prev = None
        self.attr = None
        self.pending = []                     # control strings to send before the next frame
        self.alive = True
        self.started = time.time()
        self.journal, self.journal_at, self.journal_for = [], 0, None
        self.vh = rows
        self.tick = 0
        self.want_sel_visible = False
        self.id = uuid.uuid4().hex
        self.last_seen = time.time()
        self.ansi_bbs = kind == 'telnet' and not utf8
        self.colors = 'theme'                # telnet: 'exact' (24-bit) | '256' | 'theme'; web: always 'theme'
        self.mouse = False
        self.title = None
        self.ip = None
        self.login = None                    # the viewer's Tailscale login (web sessions through tailscale serve)
        self.notice = None                   # the last service control: {name, text, color, t, busy}

    # --- input ---
    def feed(self, text):
        self.inbox.extend(parse_keys(text))
        self.last_seen = time.time()
        self.dirty.set()

    def resize(self, cols, rows):
        self.cols, self.rows = clamp_size(cols, rows)
        self.prev = None
        self.dirty.set()

    def go(self, screen, page=0):
        self.screen, self.page = screen, page
        self.want_sel_visible = screen in ('services', 'fleet')

    def flip(self, step):
        """Next/previous screen; going back lands on the last page, like reading backwards."""
        names = [n for n, _, _ in SCREENS]
        i = (names.index(self.screen) + step) % len(names)
        self.go(names[i], 10 ** 6 if step < 0 else 0)

    def page_step(self, step, flip_at_edge=True):
        if 0 <= self.page + step < self.pages:
            self.page += step
        elif flip_at_edge:
            self.flip(step)

    def view_hub(self, hub):
        """Look at another board (or come back to your own)."""
        if hub is self.hub:
            return
        self.hub.leave(self)
        self.hub = hub
        hub.join(self)
        self.sel = self.menu_sel = 0
        self.modal, self.prev = None, None
        self.go('main')

    def handle(self, k):
        if self.locked:
            return self.handle_login(k)
        d = self.hub.data
        if d is None:
            if k in ('esc', 'q', 'Q') and self.hub.remote:
                self.view_hub(self.local_hub)
            return
        svcs = d['services']
        if isinstance(k, tuple):
            kind, val = k
            if kind == 'wheel':
                if self.modal:
                    if self.modal[0] == 'chat':
                        self.current_chat().scroll = max(0, self.current_chat().scroll - val)
                    else:
                        self.modal_scroll = max(0, self.modal_scroll + val)
                elif time.time() - self.last_wheel > 0.3:     # one page per wheel gesture, no flipping
                    self.last_wheel = time.time()
                    self.page_step(1 if val > 0 else -1, flip_at_edge=False)
                return
            if kind == 'mouse':
                return self.mouse_event(*val)
            if val.startswith('svc:'):
                i = int(val[4:])
                if 0 <= i < len(svcs):
                    self.sel = i
                    self.open('svc', svcs[i]['name'])
                return
            if val.startswith('fleet:'):
                self.fleet_sel = int(val[6:])
                return self.open_fleet()
            if not val.startswith('key:'):
                return
            k = val[4:]
            if self.modal and self.modal[0] == 'chat' and k != 'esc':
                return
            if self.modal and self.modal[0] not in ('quit', 'confirm') and k not in ('esc', ' ', 'b') \
                    and not (self.modal[0] == 'svc' and k in CONTROL_KEYS):
                self.modal = None                              # a click outside the window: go there
        if self.modal:
            return self.handle_modal(k, svcs)
        key = k.lower() if isinstance(k, str) and len(k) == 1 else k
        screens = {key_: name for name, key_, _ in SCREENS}
        if key in ('up', 'down'):
            step = 1 if key == 'down' else -1
            if self.screen == 'services':
                self.sel = max(0, min(len(svcs) - 1, self.sel + step))
                self.want_sel_visible = True
            elif self.screen == 'fleet':
                self.fleet_sel = max(0, min(len(fleet_rows(View(self))) - 1, self.fleet_sel + step))
                self.want_sel_visible = True
            elif self.screen == 'main':
                self.menu_sel = (self.menu_sel + step) % len(menu_items(self))
            else:
                self.page_step(step, flip_at_edge=False)
        elif key == 'enter':
            if self.screen == 'services' and svcs:
                self.open('svc', svcs[self.sel]['name'])
            elif self.screen == 'fleet':
                self.open_fleet()
            elif self.screen == 'main':
                self.handle(menu_items(self)[self.menu_sel][0])
        elif key in ('left', '<', ','):
            self.flip(-1)
        elif key in ('right', '>', '.'):
            self.flip(1)
        elif key in (' ', 'pgdn'):
            self.page_step(1)
        elif key in ('b', 'pgup'):
            self.page_step(-1)
        elif key == 'home':
            self.page = 0
        elif key == 'end':
            self.page = 10 ** 6
        elif key == 'esc':
            if self.hub.remote:
                self.view_hub(self.local_hub)
            else:
                self.go('main')
        elif isinstance(key, str) and len(key) == 1 and key in '123456789':
            i = int(key) - 1
            if i < len(svcs):
                self.sel = i
                self.open('svc', svcs[i]['name'])
        elif key in screens:
            self.go(screens[key])
        elif key == 'c':
            self.open_chat()
        elif key == 'd':
            self.open('display')
        elif key == '?':
            self.open('help')
        elif key in ('q', 'g', 'ctrlc') and self.kind == 'telnet':
            self.open('quit')

    def open_fleet(self):
        rows = fleet_rows(View(self))
        if not 0 <= self.fleet_sel < len(rows):
            return
        name, url, _, mine = rows[self.fleet_sel]
        self.view_hub(self.local_hub if mine else self.board.remote(name, url))

    def handle_login(self, k):
        if self.modal is None:
            self.modal = ('login',)
        if k == 'enter':
            if self.board.check_password(self.input):
                self.locked, self.modal, self.input, self.login_error = False, None, '', None
                self.prev = None
            else:
                self.login_tries += 1
                self.input = ''
                left = LOGIN_TRIES - self.login_tries
                self.login_error = f'wrong password · {left} {"try" if left == 1 else "tries"} left' if left else 'goodbye.'
                time.sleep(1.5)                                # slow down guessing
                if left <= 0:
                    self.alive = False
        elif k == 'bs':
            self.input = self.input[:-1]
        elif k in ('ctrlc',) or (k in ('q', 'Q') and self.kind == 'telnet' and not self.input):
            self.alive = False
        elif isinstance(k, str) and len(k) == 1 and len(self.input) < 128:
            self.input += k

    def handle_modal(self, k, svcs):
        kind = self.modal[0]
        if kind == 'chat':
            return self.handle_chat(k)
        if kind == 'confirm':                          # start qwen? [Y/n]
            _, name, action, tight = self.modal
            if k in ('enter', 'y', 'Y'):
                self.run_control(name, action, force=bool(tight))
                self.modal = ('svc', name)
            elif k in ('esc', 'n', 'N', 'q', 'Q', 'ctrlc'):
                self.modal = ('svc', name)
            return
        if kind == 'quit':                             # disconnect? [Y/n]
            if k in ('enter', 'y', 'Y', 'q', 'Q', 'g', 'G'):
                self.goodbye = True
                self.alive = False
            elif k in ('esc', 'n', 'N', 'ctrlc'):
                self.modal = None
            return
        if k in ('esc', 'q', 'Q') or (kind == 'help' and k == '?'):
            self.modal = None
            return
        if k in ('pgdn', ' '):
            self.modal_scroll += max(1, self.vh - 6)
        elif k in ('pgup', 'b', 'B'):
            self.modal_scroll = max(0, self.modal_scroll - max(1, self.vh - 6))
        elif kind == 'svc' and svcs:
            if k in ('up', 'down'):
                self.sel = max(0, min(len(svcs) - 1, self.sel + (1 if k == 'down' else -1)))
                self.open('svc', svcs[self.sel]['name'])
            elif isinstance(k, str) and len(k) == 1 and k in '123456789' and int(k) <= len(svcs):
                self.sel = int(k) - 1
                self.open('svc', svcs[self.sel]['name'])
            elif k in ('c', 'C') and chat_capable(svcs[self.sel]):
                self.open_chat(svcs[self.sel]['name'])
            elif isinstance(k, str) and k.lower() in CONTROL_KEYS:
                sv = next((x for x in svcs if x['name'] == self.modal[1]), None)
                if sv:
                    self.ask_control(sv, k.lower())
        elif kind == 'display' and isinstance(k, str):
            p = self.prefs
            if k in ('f', 'F') and self.kind == 'web':
                p['font'] = '9x16' if p['font'] == '8x16' else '8x16'
            elif k in ('p', 'P'):
                p['pal'] = {'vga': 'amber', 'amber': 'green', 'green': 'vga'}[p['pal']]
            elif k in ('i', 'I'):
                p['ice'] = not p['ice']
            elif k in ('s', 'S') and self.kind == 'web':
                p['crt'] = not p['crt']
            elif k in ('u', 'U') and self.kind == 'telnet':
                self.utf8 = not self.utf8
                self.ansi_bbs = not self.utf8
            elif k in ('m', 'M') and self.kind == 'telnet' and self.utf8:
                self.colors = {'exact': '256', '256': 'theme', 'theme': 'exact'}[self.colors]
            else:
                return
            self.apply_prefs()

    def mouse_event(self, button, x, y, press):
        if button in (64, 65):                                   # wheel
            return self.handle(('wheel', 3 if button == 65 else -3))
        if button == 0 and press and self.prev and 0 < y <= len(self.prev) and 0 < x <= len(self.prev[0]):
            l = self.prev[y - 1][x - 1][3]
            if l and l.startswith('k:'):
                self.handle(('act', l[2:]))

    def apply_prefs(self):
        if self.kind == 'web':
            self.pending.append('\x1b]7777;prefs;' + json.dumps(self.prefs) + '\x07')
        elif self.ansi_bbs:
            self.pending.append('\x1b[?33h' if self.prefs['ice'] else '\x1b[?33l')   # SyncTERM/cterm iCE colours
        self.prev = None

    def open(self, kind, arg=None):
        self.modal, self.modal_scroll = (kind, arg), 0
        if kind == 'svc':
            self.journal_at = 0

    # --- starting and stopping services ---
    def may_control(self, sv=None):
        """Whether this viewer may control services on the board they're looking at (and this one)."""
        if not control.allowed(self.login, self.board.cfg):
            return False
        if self.hub.remote and not (self.hub.control_info or {}).get('allowed'):
            return False
        return sv is None or control.controllable(sv)

    def control_busy(self):
        n = self.notice
        return bool(n and n['busy'])

    def ask_control(self, sv, key):
        if not self.may_control(sv) or self.control_busy():
            return
        acts = control.actions_for(sv)
        action = {'s': 'stop' if 'stop' in acts else 'start', 'r': 'restart' if 'restart' in acts else None,
                  'e': 'disable' if 'disable' in acts else 'enable'}[key]
        if action:
            self.modal = ('confirm', sv['name'], action, None)

    def run_control(self, name, action, force=False):
        """Do it in the background (systemctl can take a moment); the details window shows how it went."""
        hub = self.hub
        word = {'start': 'starting', 'stop': 'stopping', 'restart': 'restarting', 'enable': 'starting',
                'disable': 'stopping'}[action]
        self.notice = {'name': name, 'text': f'{word} {name}…' if self.utf8 else f'{word} {name}...',
                       'color': 14, 't': time.time(), 'busy': True}

        def work():
            code, body = hub.control(name, action, force, self.login)
            if code == 409 and body.get('fits') is False and not force:
                self.notice = None
                if self.alive and self.hub is hub:
                    self.modal = ('confirm', name, action, body)
            elif code == 200:
                self.notice = {'name': name, 'text': f'{control.describe(action, name)}: done', 'color': 10,
                               't': time.time(), 'busy': False}
            else:
                self.notice = {'name': name, 'text': body.get('error') or f"couldn't {action} {name} (http {code})",
                               'color': 12, 't': time.time(), 'busy': False}
            self.dirty.set()

        threading.Thread(target=work, daemon=True, name='lonoboard-control').start()

    def current_notice(self, name):
        n = self.notice
        if n and n['name'] == name and (n['busy'] or time.time() - n['t'] < NOTICE_FOR):
            return n
        return None

    # --- chat ---
    def open_chat(self, service=None):
        targets = chat_targets(self.hub.data)
        if not targets:
            return
        pick = None
        if service is None and self.screen == 'services' and self.sel < len(self.hub.data['services']):
            service = self.hub.data['services'][self.sel]['name']
        if service:
            pick = next((t for t in targets if t[0] == service), None)
        if pick is None and self.chat_key and self.chat_key[0] == id(self.hub) and self.chat_key[1:] in targets:
            pick = self.chat_key[1:]
        pick = pick or targets[0]
        self.select_chat(pick)
        self.modal = ('chat', pick)

    def select_chat(self, target):
        key = (id(self.hub),) + tuple(target)
        if key not in self.chats:
            self.chats[key] = Chat(*target)
        self.chat_key = key

    def current_chat(self):
        return self.chats[self.chat_key]

    def handle_chat(self, k):
        chat = self.current_chat()
        if k == 'esc' or k == 'ctrlc':
            if chat.busy:
                chat.cancel = True
            elif k == 'esc':
                self.modal = None
        elif k == 'enter':
            text = clean(self.input).strip()
            if text and not chat.busy:
                chat.send(text, self)
                self.input = ''
        elif k == 'bs':
            self.input = self.input[:-1]
        elif k == 'tab':
            targets = chat_targets(self.hub.data)
            if len(targets) > 1 and not chat.busy:
                cur = (chat.service, chat.model)
                nxt = targets[(targets.index(cur) + 1) % len(targets)] if cur in targets else targets[0]
                self.select_chat(nxt)
                self.modal = ('chat', nxt)
        elif k == 'pgup':
            chat.scroll += max(1, self.vh - 8)
        elif k == 'pgdn':
            chat.scroll = max(0, chat.scroll - max(1, self.vh - 8))
        elif isinstance(k, str) and len(k) == 1 and len(self.input) < 2000:
            self.input += k

    # --- main loop ---
    def loop(self, write):
        self.board.sessions.add(self)
        self.hub.join(self)
        try:
            if self.ansi_bbs:
                write('\x1b[?33h' if self.prefs['ice'] else '\x1b[?33l')
            if self.kind == 'telnet' and self.utf8:
                # alternate screen (your scrollback survives) + mouse wheel and clicks (SGR reporting)
                write('\x1b[?1049h\x1b[?1000h\x1b[?1006h')
                self.mouse = True
            if self.intro:
                self.run_intro(write)
            if self.locked:
                self.modal = ('login',)
            while self.alive:
                self.dirty.wait(1.0)
                self.dirty.clear()
                if self.board.idle_timeout and time.time() - self.last_seen > self.board.idle_timeout:
                    self.restore(write, f"idle for {self.board.idle_timeout // 60} minutes - disconnected. call again any time.\r\n")
                    self.alive = False
                    break
                while self.inbox and self.alive:
                    self.handle(self.inbox.popleft())
                if not self.alive:
                    break
                if self.hub.data is None and not self.hub.remote and not self.locked:
                    continue
                out = self.frame()
                if out:
                    write(out)
                time.sleep(0.03)
            name = (self.local_hub.data or {}).get('host', {}).get('board', {}).get('name') or socket.gethostname()
            if getattr(self, 'goodbye', False):
                self.restore(write, f"disconnected from {name} · {dur(time.time() - self.started)} online\r\n")
            elif self.mouse or self.kind == 'telnet':
                self.restore(write)
        finally:
            self.alive = False
            self.hub.leave(self)
            self.board.sessions.discard(self)

    def restore(self, write, msg=''):
        """Hand the terminal back the way we found it."""
        try:
            write('\x1b[0m' + ('\x1b[?1000l\x1b[?1006l\x1b[?1049l' if self.mouse else '\x1b[2J\x1b[H') + '\x1b[?25h' + msg)
        except OSError:
            pass

    def interrupted(self):
        if self.inbox:
            self.inbox.clear()
            return True
        return not self.alive

    def run_intro(self, write):
        name = (self.hub.data or {}).get('host', {}).get('board', {}).get('name') or socket.gethostname().split('.')[0]
        write('\x1b[0m\x1b[2J\x1b[H\x1b[?25h')
        for text, f in (('ATZ', 7), ('OK', 7), (f'ATDT {name.upper()}', 7), ('', 7), (f'connecting to {name}...', 15), ('', 7)):
            for ch in text:
                if self.interrupted():
                    return
                write(f'\x1b[0;{37 if f == 7 else 97 if not self.ansi_bbs else "1;37"}m{ch}')
                time.sleep(0.028 if text.startswith('AT') else 0.006)
            write('\r\n')
            time.sleep(0.26 if text == 'OK' else 0.9 if text.startswith('ATDT') else 0.12)
        if self.locked:
            return
        for _ in range(100):
            if self.hub.data is not None or self.interrupted():
                break
            time.sleep(0.05)
        if self.hub.data is None or self.interrupted():
            return
        # paint the first screen progressively, the way it would arrive over a line
        out = self.frame()
        step = max(800, len(out) // 60)
        for i in range(0, len(out), step):
            if self.interrupted():
                self.prev = None
                return
            write(out[i:i + step])
            time.sleep(0.015)

    # --- rendering ---
    def layout_w(self):
        return 160 if self.cols >= 160 else 80 if self.cols >= 80 else self.cols

    def frame(self):
        self.tick += 1
        scr, cursor = self.render()
        return encode(self, scr, cursor)

    def _blank(self):
        scr = Screen(self.cols)
        for y in range(self.rows):
            scr.row(y)
        return scr

    def render(self):
        W, cols, rows = self.layout_w(), self.cols, self.rows
        X0 = (cols - W) // 2
        if self.locked:                            # nothing from the board until you've logged in
            scr = self._blank()
            name = self.board.cfg['board']['name'] or socket.gethostname().split('.')[0]
            center_segs(scr, 1, [('░▒▓', 1), (f' {name} ', 15, 1), ('▓▒░', 1)])
            from lonoboard.board.screens import build_login
            box, cur = build_login(_LoginView(self, name), W)
            mx, my = X0 + (W - box.w) // 2, max(3, (rows - box.h) // 2)
            scr.blit(box, mx, my)
            return scr, (mx + cur[0], my + cur[1])
        if self.hub.data is None:                  # a remote board we haven't heard from yet
            scr = self._blank()
            msg = self.hub.error or f'connecting to {self.hub.label}...'
            center_segs(scr, rows // 2, [(msg, 15 if not self.hub.error else 12), ('   [esc] back', 8)])
            return scr, None
        self.title = f"{self.hub.data['host']['board']['name']} · lonoboard"
        v = View(self)
        # one screen per window; anything taller is split into pages with a more-prompt. The footer
        # shows page keys only when there are pages, and they can take another row - so settle the
        # two together (at most twice round: page keys only ever make paging more necessary)
        paged = None
        while paged != (self.pages > 1):
            paged = self.pages > 1
            footer, cur_x, cur_y = build_footer(v, W, rows)
            self.vh = vh = rows - footer.h
            content = build_screen(v, W, vh)
            per = vh if content.h <= vh else max(1, vh - 1)
            self.pages = max(1, -(-content.h // per))
        if self.want_sel_visible and self.sel_row is not None:
            self.page = self.sel_row // per
        self.want_sel_visible = False
        self.page = max(0, min(self.page, self.pages - 1))
        scr = self._blank()
        top = self.page * per
        for y in range(min(per, vh)):
            if top + y < content.h:
                r = scr.rows[y]
                for i, c in enumerate(content.rows[top + y]):
                    r[X0 + i] = c
        if self.pages > 1:
            scr.blit(more_prompt(v, W, self.page, self.pages), X0, vh - 1)
        scr.blit(footer, X0, vh)
        scr.blit(build_status(v, W), X0, rows - 1)
        cursor = (X0 + cur_x, vh + cur_y) if cur_x is not None else None
        if self.modal:
            m, mcur = build_modal(v, W, vh)
            if m is not None:
                mx, my = X0 + (W - m.w) // 2, max(0, (vh - m.h) // 2)
                for yy in range(my + 1, min(rows - 1, my + m.h + 1)):        # drop shadow: dim what's behind
                    r = scr.rows[yy]
                    for xx in range(mx + 2, min(cols, mx + m.w + 2)):
                        if xx >= mx + m.w or yy >= my + m.h:
                            c = r[xx]
                            r[xx] = (c[0], 8, 0, None)
                scr.blit(m, mx, my)
                cursor = (mx + mcur[0], my + mcur[1]) if mcur else None
        return scr, cursor


class _LoginView:
    """Just enough of a View to draw the login window without touching the board's data."""

    def __init__(self, sess, name):
        self.s, self.name, self.utf8 = sess, name, sess.utf8 or sess.kind == 'web'
