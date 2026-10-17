"""The telnet server: option negotiation (echo, suppress-go-ahead, window size, terminal type),
UTF-8 or CP437 by terminal type, and a Session per connection."""
import socket
import socketserver
import threading
import time

from lonoboard.board.session import Session

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
SB_MAX = 256                          # longest subnegotiation kept (window size and terminal type are short)
ECHO, SGA, TTYPE, NAWS = 1, 3, 24, 31
UTF8_TERMS = ('xterm', 'screen', 'tmux', 'rxvt', 'alacritty', 'kitty', 'foot', 'wezterm', 'vt1', 'vt2', 'linux',
              'putty', 'ghostty', 'konsole', 'gnome', 'iterm', 'st-', 'contour')
# terminals known to do 24-bit colour; '*-256color' gets the 256-colour palette; others their own 16
TRUECOLOR_TERMS = ('ghostty', 'kitty', 'alacritty', 'foot', 'wezterm', 'iterm', 'konsole', 'contour', 'direct', 'truecolor')


class TelnetHandler(socketserver.BaseRequestHandler):
    def setup(self):
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sess = None
        self.cols, self.rows, self.ttype = 80, 24, None
        self.buf = bytearray()
        self.state, self.sb = 'data', bytearray()
        self.lock = threading.Lock()

    def send(self, b):
        with self.lock:
            self.request.sendall(b)

    def write(self, text):
        enc = 'utf-8' if self.sess.utf8 else 'cp437'
        self.send(text.encode(enc, 'replace').replace(b'\xff', b'\xff\xff'))

    def handle(self):
        board = self.server.board
        if board.full(self.client_address[0]):
            self.send(b'too many connections, try again later.\r\n')
            return
        self.send(bytes([IAC, WILL, ECHO, IAC, WILL, SGA, IAC, DO, NAWS, IAC, DO, TTYPE]))
        self.request.settimeout(0.6)
        deadline = time.time() + 0.8
        while time.time() < deadline and self.ttype is None:
            try:
                data = self.request.recv(1024)
            except socket.timeout:
                break
            if not data:
                return
            self.parse(data)
        self.request.settimeout(None)
        tt = (self.ttype or '').lower()
        utf8 = any(t in tt for t in UTF8_TERMS)
        self.sess = Session(board, 'telnet', self.cols, self.rows, utf8=utf8, intro=board.intro)
        self.sess.ip = self.client_address[0]
        self.sess.colors = ('exact' if any(t in tt for t in TRUECOLOR_TERMS) else '256' if '256' in tt else 'theme') if utf8 else 'theme'
        if self.buf:
            self.sess.feed(self.decode(bytes(self.buf)))
            self.buf.clear()
        reader = threading.Thread(target=self.read_loop, daemon=True)
        reader.start()
        try:
            self.sess.loop(self.write)
        except OSError:
            pass
        finally:
            self.sess.alive = False

    def decode(self, b):
        return b.decode('utf-8' if self.sess and self.sess.utf8 else 'cp437', 'replace')

    def read_loop(self):
        try:
            while self.sess.alive:
                data = self.request.recv(4096)
                if not data:
                    break
                self.parse(data)
                if self.buf:
                    self.sess.feed(self.decode(bytes(self.buf)))
                    self.buf.clear()
        except OSError:
            pass
        self.sess.alive = False
        self.sess.dirty.set()

    def parse(self, data):
        for c in data:
            st = self.state
            if st == 'data':
                if c == IAC:
                    self.state = 'iac'
                else:
                    self.buf.append(c)
            elif st == 'iac':
                if c == IAC:
                    self.buf.append(c)
                    self.state = 'data'
                elif c in (DO, DONT, WILL, WONT):
                    self.opt_cmd, self.state = c, 'opt'
                elif c == SB:
                    self.sb.clear()
                    self.state = 'sb'
                else:
                    self.state = 'data'
            elif st == 'opt':
                self.option(self.opt_cmd, c)
                self.state = 'data'
            elif st == 'sb':
                if c == IAC:
                    self.state = 'sb_iac'
                elif len(self.sb) < SB_MAX:
                    self.sb.append(c)
            elif st == 'sb_iac':
                if c == SE:
                    self.subneg(bytes(self.sb))
                    self.state = 'data'
                else:
                    if len(self.sb) < SB_MAX:
                        self.sb.append(c)
                    self.state = 'sb'

    def option(self, cmd, opt):
        if cmd == WILL and opt == TTYPE:
            self.send(bytes([IAC, SB, TTYPE, 1, IAC, SE]))
        elif cmd == WILL and opt not in (NAWS, TTYPE):
            self.send(bytes([IAC, DONT, opt]))
        elif cmd == DO and opt not in (ECHO, SGA):
            self.send(bytes([IAC, WONT, opt]))

    def subneg(self, sb):
        if sb[:1] == bytes([NAWS]) and len(sb) >= 5:
            self.cols, self.rows = (sb[1] << 8) | sb[2], (sb[3] << 8) | sb[4]
            if self.sess and self.cols and self.rows:
                self.sess.resize(self.cols, self.rows)
        elif sb[:2] == bytes([TTYPE, 0]):
            self.ttype = sb[2:].decode('ascii', 'replace')


class TelnetServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, board):
        self.board = board
        self.address_family = socket.AF_INET6 if ':' in address[0] else socket.AF_INET
        super().__init__(address, TelnetHandler)


def serve(board, host, port):
    srv = TelnetServer((host, port), board)
    threading.Thread(target=srv.serve_forever, daemon=True, name='lonoboard-telnet').start()
    return srv
