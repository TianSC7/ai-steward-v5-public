# -*- coding: utf-8 -*-
"""Restart caddy for MES-TLS-8900: terminate stale caddy.exe, reset task, start, verify cert."""
import ctypes
import socket
import ssl
import subprocess
import sys
import time

from cryptography import x509


def find_caddy_pids():
    out = subprocess.run(["tasklist", "/fi", "IMAGENAME eq caddy.exe", "/fo", "csv", "/nh"],
                         capture_output=True).stdout.decode("gbk", "ignore")
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "caddy.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def terminate(pid):
    PROCESS_TERMINATE = 0x0001
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    ok = k32.TerminateProcess(h, 1)
    k32.CloseHandle(h)
    return bool(ok)


def port_open(port, timeout=1.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def peer_cert(port):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname="mes.example.com") as tls:
            der = tls.getpeercert(binary_form=True)
    return x509.load_der_x509_certificate(der)


def main():
    pids = find_caddy_pids()
    print("caddy pids before:", pids)
    for pid in pids:
        print("terminate", pid, "->", terminate(pid))
    time.sleep(2)
    print("caddy pids after kill:", find_caddy_pids())

    subprocess.run(["schtasks", "/end", "/tn", "MES-TLS-8900"], capture_output=True)
    time.sleep(1)
    subprocess.run(["schtasks", "/run", "/tn", "MES-TLS-8900"], capture_output=True)

    up = False
    for _ in range(30):
        time.sleep(1)
        if port_open(8900):
            up = True
            break
    print("port 8900 up:", up)
    print("caddy pids now:", find_caddy_pids())
    if up:
        try:
            c = peer_cert(8900)
            print("served issuer:", c.issuer.rfc4514_string())
            print("served not_after:", c.not_valid_after_utc)
        except Exception as e:
            print("cert probe failed:", e)


if __name__ == "__main__":
    main()
