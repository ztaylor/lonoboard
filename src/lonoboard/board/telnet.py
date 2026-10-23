"""The telnet server: option negotiation (echo, suppress-go-ahead, window size, terminal type),
UTF-8 or CP437 by terminal type, and a Session per connection.

Behind `tailscale serve --proxy-protocol` every connection comes from this machine and starts with a PROXY
header naming the real sender, so the board can ask Tailscale who they are (for [control])."""
import ipaddress
import socket
import socketserver
import threading
import time

from lonoboard import control
from lonoboard.board.session import Session

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
SB_MAX = 256                          # longest subnegotiation kept (window size and terminal type are short)
ECHO, SGA, TTYPE, NAWS = 1, 3, 24, 31
UTF8_TERMS = ('xterm', 'screen', 'tmux', 'rxvt', 'alacritty', 'kitty', 'foot', 'wezterm', 'vt1', 'vt2', 'linux',
              'putty', 'ghostty', 'konsole', 'gnome', 'iterm', 'st-', 'contour')
# terminals known to do 24-bit colour; '*-256color' gets the 256-colour palette; others their own 16
TRUECOLOR_TERMS = ('ghostty', 'kitty', 'alacritty', 'foot', 'wezterm', 'iterm', 'konsole', 'contour', 'direct', 'truecolor')
PROXY_V1, PROXY_V2 = b'PROXY ', b'\r\n\r\n\x00\r\nQUIT\n'
PROXY_V1_MAX = 107                    # the longest v1 header, by the spec
PROXY_WAIT = 0.3                      # tailscale serve sends the header as soon as it connects


def peek(sock, n, deadline, stop=None):
    """Up to n bytes waiting on the socket, without taking them: fewer if no more come by the deadline,
    or once `stop` has arrived."""
    got = b''
    while len(got) < n and not (stop and stop in got):
        left = deadline - time.time()
        if left <= 0:
            break
        sock.settimeout(left)
        try:
            more = sock.recv(n, socket.MSG_PEEK)
        except socket.timeout:
            break
        if not more:
            break
        if len(more) == len(got):
            time.sleep(0.01)                               # part of it is here: give the rest a moment
        got = more
    return got


def take(sock, n):
    got = b''
    while len(got) < n:
        more = sock.recv(n - len(got))
        if not more:
            raise ValueError('connection closed inside the PROXY header')
        got += more
    return got


def read_proxy(sock, wait=PROXY_WAIT):
    """The sender's address from a PROXY protocol header (v1 or v2) at the start of a connection.

    None if the connection doesn't start with one (it's left untouched) or the header names no address.
    ValueError if it starts like one but is broken."""
    deadline = time.time() + wait
    head = peek(sock, len(PROXY_V2), deadline)
    if head.startswith(PROXY_V1):
        line = peek(sock, PROXY_V1_MAX, deadline, stop=b'\r\n')
        end = line.find(b'\r\n')
        if end < 0:
            raise ValueError('PROXY header too long')
        parts = take(sock, end + 2)[:-2].split(b' ')
        if len(parts) == 6 and parts[1] in (b'TCP4', b'TCP6'):
            return str(ipaddress.ip_address(parts[2].decode('ascii')))
        if parts[1:2] == [b'UNKNOWN']:
            return None
        raise ValueError('bad PROXY header')
    if head == PROXY_V2:
        fixed = take(sock, 16)
        body = take(sock, int.from_bytes(fixed[14:16], 'big'))
        if fixed[12] >> 4 != 2:
            raise ValueError('bad PROXY header version')
        if fixed[12] & 0x0f == 0:                          # LOCAL: a health check from the proxy itself
            return None
        family = fixed[13] >> 4
        if family == 1 and len(body) >= 12:
            return str(ipaddress.ip_address(body[:4]))
        if family == 2 and len(body) >= 36:
            return str(ipaddress.ip_address(body[:16]))
        return None
    return None


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
        ip = self.client_address[0]
        if ipaddress.ip_address(ip).is_loopback:          # tailscale serve forwards from here
            try:
                ip = read_proxy(self.request) or ip
            except (OSError, ValueError):
                return
        if board.full(ip):
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
        self.sess.ip = ip
        self.sess.login = control.identity(ip, {})
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
