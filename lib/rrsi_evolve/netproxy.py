# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Network for sandboxed trials: an allowlist HTTP CONNECT proxy.

A trial in a bwrap sandbox gets its own network namespace (`--unshare-net`):
no route out, and none of the host's abstract unix sockets (X11, D-Bus, the
user's systemd manager). Pathname sockets are the sandbox layout's job
(/run is empty there, others are masked). The only way out is this proxy.
It listens on a unix socket in the run's private dir, which is bound into
the sandbox read-only; `FORWARDER` (run inside the sandbox before the
policy) listens on the sandbox's own 127.0.0.1 and passes each connection
to it. The policy and its tools get HTTPS_PROXY pointing at the forwarder;
the proxy only opens CONNECT tunnels to allowed host names on allowed ports,
never to IP literals, loopback or private addresses, except the model
gateway the user configured (ANTHROPIC_*BASE_URL): a remote one is allowed
as configured, a loopback one is reached through a plain port forward. When
the host itself sits behind an HTTPS proxy, tunnels go through it.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import socket
import threading
from pathlib import Path
from urllib.parse import urlparse

# Claude Code's API and sign-in hosts (first-party), plus the cloud providers
# when the environment routes the model there.
DEFAULT_ALLOW = ("api.anthropic.com", "*.anthropic.com", "anthropic.com",
                 "claude.ai", "*.claude.ai", "claude.com", "*.claude.com")


def default_allow(env=None) -> list[str]:
    env = os.environ if env is None else env
    allow = list(DEFAULT_ALLOW)
    for var in BASE_URL_VARS:
        host = urlparse(env.get(var) or "").hostname
        if host:
            allow.append(host)
    if env.get("CLAUDE_CODE_USE_BEDROCK"):
        allow += ["*.amazonaws.com", "*.aws"]
    if env.get("CLAUDE_CODE_USE_VERTEX"):
        allow += ["*.googleapis.com", "oauth2.googleapis.com"]
    if env.get("CLAUDE_CODE_USE_FOUNDRY"):
        allow += ["*.azure.com", "*.microsoft.com", "*.microsoftonline.com"]
    return allow


BASE_URL_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
                 "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_FOUNDRY_BASE_URL")


def gateways(env=None, reserved=(3128,)) -> tuple[set, dict, dict]:
    """The model gateways the user configured: (host, port) pairs the proxy
    opens even on a private address or another port; loopback ones
    ({sandbox port: (host, port)}), which the sandbox reaches by a plain port
    forward (its own loopback is not the host's); and the base-URL variables
    to rewrite for the trial when a loopback gateway's port is privileged
    (a sandbox cannot listen below 1024)."""
    env = os.environ if env is None else env
    trusted, forwards, rewrite = set(), {}, {}
    for var in BASE_URL_VARS:
        raw = env.get(var) or ""
        u = urlparse(raw)
        if not u.hostname:
            continue
        try:
            port = u.port or (80 if u.scheme == "http" else 443)
        except ValueError:
            raise SystemExit(f"invalid port in {var}={raw!r}")
        if _is_loopback(u.hostname):
            inside = port
            if port < 1024 or port in reserved:
                inside = 10000 + port
                while inside in reserved or inside in forwards:
                    inside += 1
                user = u.netloc.rsplit("@", 1)[0] + "@" if "@" in u.netloc else ""
                host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
                rewrite[var] = u._replace(netloc=f"{user}{host}:{inside}").geturl()
            forwards[inside] = (u.hostname, port)
        else:
            trusted.add((u.hostname.lower(), port))
    return trusted, forwards, rewrite


_HOSTNAME = __import__("re").compile(r"[a-z0-9_]([a-z0-9_-]{0,62})(\.[a-z0-9_]([a-z0-9_-]{0,62}))*")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _is_loopback(host: str) -> bool:
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _no_proxy(host: str, port: int, spec: str) -> bool:
    for raw in (spec or "").split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if item == "*":
            return True
        name, _, p = item.partition(":") if item.count(":") == 1 else (item, "", "")
        if p and p != str(port):
            continue
        name = name.lstrip("*")
        if host == name.lstrip(".") or (name.startswith(".") and host.endswith(name)) \
                or host.endswith("." + name):
            return True
    return False


def upstream(env=None):
    """The host's own HTTPS proxy, if any: (host, port, Proxy-Authorization
    header or None, NO_PROXY), so trials keep using it."""
    env = os.environ if env is None else env
    url = env.get("HTTPS_PROXY") or env.get("https_proxy") or env.get("HTTP_PROXY") \
        or env.get("http_proxy")
    if not url:
        return None
    u = urlparse(url if "://" in url else "http://" + url)
    if not u.hostname or u.scheme not in ("http", "https"):
        return None
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        raise SystemExit(f"invalid proxy URL {url!r}")
    auth = None
    if u.username:
        from base64 import b64encode
        from urllib.parse import unquote
        cred = f"{unquote(u.username)}:{unquote(u.password or '')}".encode()
        auth = "Basic " + b64encode(cred).decode()
    return (u.hostname, port, auth, env.get("NO_PROXY") or env.get("no_proxy") or "",
            u.scheme == "https")


class Proxy:
    """CONNECT-only proxy on a unix socket; one thread per tunnel. With
    `forwards`, also a raw TCP forward per port (its own unix socket
    `fwd-<port>.sock` next to the proxy's) to a loopback gateway."""

    def __init__(self, sock_path: Path, allow, ports=(443,), trusted=(), forwards=None,
                 via=None):
        self.path = str(sock_path)
        self.allow = [a.lower() for a in allow]
        self.ports = {int(p) for p in ports}
        self.trusted = {(h.lower(), int(p)) for h, p in trusted}
        self.via = via
        self.denied: set[str] = set()
        self._lock = threading.Lock()
        self._stop = False
        self._srvs = []
        self.forwards = {}
        self._listen(self.path, self._handle)
        for port, target in (forwards or {}).items():
            fp = str(Path(self.path).parent / f"fwd-{int(port)}.sock")
            self.forwards[int(port)] = fp
            self._listen(fp, lambda c, t=target: self._forward(c, t))

    def _listen(self, path: str, handler) -> None:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(64)
        self._srvs.append((srv, path))
        threading.Thread(target=self._serve, args=(srv, handler), daemon=True).start()

    def allowed(self, host: str, port: int) -> bool:
        host = host.lower().rstrip(".")
        if (host.strip("[]"), port) in self.trusted:
            return True
        if port not in self.ports or not host or _is_ip(host) or host == "localhost" \
                or not _HOSTNAME.fullmatch(host):
            return False
        return any(fnmatch.fnmatchcase(host, pat) for pat in self.allow)

    def _deny(self, what: str) -> None:
        with self._lock:
            self.denied.add(what)

    def close(self) -> None:
        self._stop = True
        for srv, path in self._srvs:
            try:
                srv.close()
            except OSError:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass

    def _serve(self, srv, handler) -> None:
        while not self._stop:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    def _forward(self, conn: socket.socket, target) -> None:
        up = None
        try:
            up = socket.create_connection(target, timeout=30)
            up.settimeout(None)
            _pipe(conn, up)
        except OSError as e:
            self._deny(f"{target[0]}:{target[1]} (forward failed: {e.__class__.__name__})")
        finally:
            for s in (conn, up):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def _open(self, host: str, port: int) -> tuple[socket.socket | None, str | None]:
        """A connection to host:port (directly, or through the host's proxy)
        or the reason there is none."""
        via = self.via
        if via and not _no_proxy(host, port, via[3]):
            if (host, port) not in self.trusted:
                # the upstream resolves the name; check what it resolves to
                # here too, when we can (no DNS here is no answer, not a no)
                try:
                    addrs = {ipaddress.ip_address(a[4][0].split("%")[0])
                             for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
                except (OSError, ValueError):
                    addrs = set()
                if addrs and all(a.is_private or a.is_loopback or a.is_link_local
                                 or a.is_reserved for a in addrs):
                    return None, f"resolves to a non-public address {min(addrs)}"
            up = socket.create_connection((via[0], via[1]), timeout=30)
            if len(via) > 4 and via[4]:
                import ssl
                up = ssl.create_default_context().wrap_socket(up, server_hostname=via[0])
            req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if via[2]:
                req += f"Proxy-Authorization: {via[2]}\r\n"
            up.sendall((req + "\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 16384:
                chunk = up.recv(4096)
                if not chunk:
                    break
                head += chunk
            status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 200" not in status[:13]:
                up.close()
                return None, f"upstream proxy answered {status[:60]!r}"
            return up, None
        up = socket.create_connection((host, port), timeout=30)
        if (host, port) not in self.trusted:
            # the name resolved to a public address only
            addr = ipaddress.ip_address(up.getpeername()[0])
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                up.close()
                return None, f"resolves to a non-public address {addr}"
        return up, None

    def _handle(self, conn: socket.socket) -> None:
        up = None
        try:
            conn.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 16384:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = line.split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                self._deny(f"{parts[1] if len(parts) > 1 else '?'} (plain HTTP is not "
                           f"proxied, only HTTPS)")
                conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            host, _, port = parts[1].rpartition(":")
            host = host.strip("[]").lower().rstrip(".")
            try:
                port_n = int(port)
            except ValueError:
                port_n = -1
            if not self.allowed(host, port_n):
                self._deny(f"{host}:{port} (not in the allowlist)")
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            try:
                up, why = self._open(host, port_n)
            except OSError as e:
                up, why = None, f"connect failed: {e.__class__.__name__}"
            if up is None:
                self._deny(f"{host}:{port} ({why})")
                code = b"403 Forbidden" if why and why.startswith("resolves") else b"502 Bad Gateway"
                conn.sendall(b"HTTP/1.1 " + code + b"\r\n\r\n")
                return
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            rest = head.split(b"\r\n\r\n", 1)[1]
            if rest:
                up.sendall(rest)
            conn.settimeout(None)
            up.settimeout(None)
            _pipe(conn, up)
        except OSError:
            pass
        finally:
            for s in (conn, up):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass


def _pipe(a: socket.socket, b: socket.socket) -> None:
    def one(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    t = threading.Thread(target=one, args=(b, a), daemon=True)
    t.start()
    one(a, b)
    t.join()


# Runs inside the sandbox: `python3 -I -c FORWARDER <port>=<unix socket> ...
# -- <cmd...>`. Listens on 127.0.0.1:<port> (and ::1 where it can; the
# sandbox's own loopback) for each pair, forks a server that passes every
# connection to the unix socket, and execs the command.
FORWARDER = r"""
import os, socket, sys, threading
i = sys.argv.index("--")
pairs, cmd = sys.argv[1:i], sys.argv[i + 1:]
srvs = []
for pair in pairs:
    port, path = pair.split("=", 1)
    for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, int(port)))
            s.listen(64)
            srvs.append((s, path))
        except OSError:
            if fam == socket.AF_INET:
                raise
def pipe(a, b):
    def one(s, d):
        try:
            while True:
                x = s.recv(65536)
                if not x:
                    break
                d.sendall(x)
        except OSError:
            pass
        finally:
            try:
                d.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    t = threading.Thread(target=one, args=(b, a), daemon=True)
    t.start(); one(a, b); t.join()
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass
def handle(c, path):
    try:
        u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        u.connect(path)
    except OSError:
        c.close()
        return
    pipe(c, u)
def serve(srv, path):
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            os._exit(0)
        threading.Thread(target=handle, args=(c, path), daemon=True).start()
if os.fork() == 0:
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    for srv, path in srvs[1:]:
        threading.Thread(target=serve, args=(srv, path), daemon=True).start()
    serve(*srvs[0])
for srv, _ in srvs:
    srv.close()
os.execvp(cmd[0], cmd)
"""
