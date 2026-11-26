#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal ACME DNS-01 client for one domain, manual TXT mode.

Modes:
  new      create ACME account + order, print the TXT record to add
  check    query public DNS-over-HTTPS for the TXT record
  finish   trigger validation, finalize order, download cert, install for Caddy
  status   show current state + installed cert expiry

Usage:
  python acme_dns01.py new    --domain mes.example.com --email admin@example.com
  python acme_dns01.py check
  python acme_dns01.py finish --restart
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import socket
import subprocess
import sys
import time

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

LE_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
BASE = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"
STATE_DIR = os.path.join(BASE, "state", "acme")
CERT_ROOT = os.path.join(BASE, "state", "certs")
STATE_FILE = os.path.join(STATE_DIR, "order.json")
ACCOUNT_KEY = os.path.join(STATE_DIR, "account_key.pem")
CERT_KEY = os.path.join(STATE_DIR, "cert_key.pem")

DO_SERVERS = [
    ("cloudflare", "https://cloudflare-dns.com/dns-query"),
    ("google", "https://dns.google/resolve"),
    ("alidns", "https://dns.alidns.com/resolve"),
]


# ---------------------------------------------------------------- helpers

def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def log(*a):
    print(*a, flush=True)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- account key

def load_or_create_account_key():
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(ACCOUNT_KEY):
        with open(ACCOUNT_KEY, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    with open(ACCOUNT_KEY, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    return key


def jwk_public(key):
    nums = key.public_key().public_numbers()
    return {
        "crv": "P-256",
        "kty": "EC",
        "x": b64u(nums.x.to_bytes(32, "big")),
        "y": b64u(nums.y.to_bytes(32, "big")),
    }


def jwk_thumbprint(jwk):
    canonical = json.dumps(jwk, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return b64u(hashlib.sha256(canonical).digest())


# ---------------------------------------------------------------- ACME session

class Acme:
    def __init__(self, key):
        self.key = key
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "aa-acme-dns01/1.0"})
        self.dir = self.http.get(LE_DIRECTORY, timeout=30).json()
        self.kid = None

    def nonce(self):
        r = self.http.head(self.dir["newNonce"], timeout=30)
        return r.headers["Replay-Nonce"]

    def _sign(self, url, payload, kid=None, use_jwk=False):
        jwk = jwk_public(self.key)
        protected = {"alg": "ES256", "nonce": self.nonce(), "url": url}
        if use_jwk:
            protected["jwk"] = jwk
        else:
            protected["kid"] = kid or self.kid
        p64 = b64u(json.dumps(protected, separators=(",", ":")).encode())
        if payload is None:
            pl64 = ""
        else:
            pl64 = b64u(json.dumps(payload, separators=(",", ":")).encode())
        der = self.key.sign((p64 + "." + pl64).encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return {"protected": p64, "payload": pl64, "signature": b64u(sig)}

    def post(self, url, payload, use_jwk=False):
        body = self._sign(url, payload, use_jwk=use_jwk)
        r = self.http.post(url, json=body, timeout=60,
                           headers={"Content-Type": "application/jose+json"})
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:500]
            raise RuntimeError("ACME %s -> %s: %s" % (url, r.status_code, detail))
        return r

    def get(self, url):
        return self.post(url, None)

    def register(self, email):
        payload = {"termsOfServiceAgreed": True}
        if email:
            payload["contact"] = ["mailto:" + email]
        r = self.post(self.dir["newAccount"], payload, use_jwk=True)
        self.kid = r.headers.get("Location")
        return self.kid


# ---------------------------------------------------------------- DNS checks

def doh_txt(name):
    results = {}
    for tag, base in DO_SERVERS:
        try:
            r = requests.get(base, params={"name": name, "type": "TXT"},
                             headers={"Accept": "application/dns-json"}, timeout=15)
            j = r.json()
            vals = []
            for ans in j.get("Answer") or []:
                if ans.get("type") == 16:
                    vals.append(ans.get("data", "").strip('"'))
            results[tag] = vals
        except Exception as e:
            results[tag] = ["ERR: %s" % e]
    return results


def doh_caa(name):
    try:
        r = requests.get(DO_SERVERS[1][1], params={"name": name, "type": "CAA"},
                         headers={"Accept": "application/dns-json"}, timeout=15)
        j = r.json()
        return [(a.get("data") or "") for a in (j.get("Answer") or []) if a.get("type") == 257]
    except Exception as e:
        return ["ERR: %s" % e]


# ---------------------------------------------------------------- modes

def mode_new(args):
    key = load_or_create_account_key()
    acme = Acme(key)
    acme.register(args.email)
    log("ACCOUNT: %s" % acme.kid)

    resp = acme.post(acme.dir["newOrder"], {
        "identifiers": [{"type": "dns", "value": args.domain}]
    })
    order = resp.json()
    order_url = resp.headers.get("Location")

    txts = []
    for authz_url in order["authorizations"]:
        authz = acme.get(authz_url).json()
        if authz.get("status") == "valid":
            continue
        ident = (authz.get("identifier") or {}).get("value") or args.domain
        chal = None
        for c in authz.get("challenges") or []:
            if c.get("type") == "dns-01":
                chal = c
                break
        if not chal:
            raise RuntimeError("no dns-01 challenge for %s" % ident)
        key_auth = chal["token"] + "." + jwk_thumbprint(jwk_public(key))
        txt_value = b64u(hashlib.sha256(key_auth.encode()).digest())
        txts.append({
            "identifier": ident,
            "name": "_acme-challenge." + ident,
            "value": txt_value,
            "authz_url": authz_url,
            "challenge_url": chal["url"],
        })

    state = {
        "domain": args.domain,
        "account_url": acme.kid,
        "order_url": order_url,
        "finalize_url": order.get("finalize"),
        "status": order.get("status"),
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "txts": txts,
    }
    # orders from LE carry the URL in the Location header, not the body
    save_json(STATE_FILE, state)

    log("")
    log("=== DNS-01 CHALLENGE ===")
    for t in txts:
        log("TXT_NAME=%s" % t["name"])
        log("TXT_VALUE=%s" % t["value"])
    log("")
    caa = doh_caa(args.domain)
    log("CAA records on %s: %s" % (args.domain, caa if caa else "none"))
    log("")
    log("Add the TXT record(s) above in the DNS panel of example.com,")
    log("then run:  python acme_dns01.py check")


def mode_check(args):
    st = load_json(STATE_FILE)
    if not st:
        raise SystemExit("no order state; run mode new first")
    ok = True
    for t in st["txts"]:
        log("--- %s ---" % t["name"])
        res = doh_txt(t["name"])
        found = False
        for tag, vals in res.items():
            match = any(v.strip().strip('"') == t["value"] for v in vals)
            found = found or match
            shown = [v[:60] + ("..." if len(v) > 60 else "") for v in vals]
            log("  %-10s match=%s %s" % (tag, "YES" if match else "no", shown))
        if not found:
            ok = False
    log("")
    log("DNS_READY=%s" % ("YES" if ok else "NO"))


def poll_authz(acme, url, timeout=120):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        a = acme.get(url).json()
        last = a
        if a.get("status") in ("valid", "invalid"):
            return a
        time.sleep(3)
    return last


def mode_finish(args):
    st = load_json(STATE_FILE)
    if not st:
        raise SystemExit("no order state; run mode new first")
    if not st.get("order_url"):
        raise SystemExit("state missing order_url; rerun mode new")

    key = load_or_create_account_key()
    acme = Acme(key)
    acme.kid = st.get("account_url") or acme.register(args.email or "admin@example.com")

    if not args.force:
        missing = []
        for t in st["txts"]:
            res = doh_txt(t["name"])
            if not any(any(v.strip().strip('"') == t["value"] for v in vals) for vals in res.values()):
                missing.append(t["name"])
        if missing:
            raise SystemExit("TXT not visible yet at public resolvers: %s (use --force to skip)" % missing)

    for t in st["txts"]:
        acme.post(t["challenge_url"], {})

    for t in st["txts"]:
        a = poll_authz(acme, t["authz_url"])
        log("AUTHZ %s status=%s" % (t["identifier"], a.get("status")))
        if a.get("status") != "valid":
            errs = []
            for c in a.get("challenges") or []:
                e = c.get("error")
                if e:
                    errs.append("%s: %s" % (c.get("type"), e))
            raise SystemExit("authorization failed: %s" % (errs or a.get("status")))

    order = acme.get(st["order_url"]).json()
    log("ORDER status=%s" % order.get("status"))

    if order.get("status") in ("pending", "ready"):
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(CERT_KEY):
            with open(CERT_KEY, "rb") as f:
                cert_key = serialization.load_pem_private_key(f.read(), password=None)
        else:
            cert_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            with open(CERT_KEY, "wb") as f:
                f.write(cert_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption()))
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, st["domain"])]))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(st["domain"])]), critical=False)
               .sign(cert_key, hashes.SHA256()))
        der = csr.public_bytes(serialization.Encoding.DER)
        order = acme.post(st["finalize_url"], {"csr": b64u(der)}).json()
        log("FINALIZE status=%s" % order.get("status"))
    else:
        if not order.get("certificate"):
            raise SystemExit("order not pending and no certificate: %s" % order.get("status"))

    deadline = time.time() + 120
    while time.time() < deadline and not order.get("certificate"):
        time.sleep(3)
        order = acme.get(st["order_url"]).json()
        log("ORDER status=%s" % order.get("status"))
    if not order.get("certificate"):
        raise SystemExit("certificate not issued in time: %s" % order.get("status"))

    pem = acme.get(order["certificate"]).text
    if "BEGIN CERTIFICATE" not in pem:
        raise SystemExit("unexpected certificate response")

    cert_dir = os.path.join(CERT_ROOT, st["domain"])
    os.makedirs(cert_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name in ("fullchain.pem", "key.pem"):
        p = os.path.join(cert_dir, name)
        if os.path.exists(p):
            os.replace(p, p + ".bak-" + stamp)
    fullchain = os.path.join(cert_dir, "fullchain.pem")
    keypath = os.path.join(cert_dir, "key.pem")
    with open(fullchain, "w", encoding="ascii", newline="\n") as f:
        f.write(pem.strip() + "\n")
    with open(CERT_KEY, "rb") as f:
        keypem = f.read()
    with open(keypath, "wb") as f:
        f.write(keypem)

    cert = x509.load_pem_x509_certificate(open(fullchain, "rb").read())
    log("")
    log("CERT_INSTALLED=%s" % fullchain)
    log("KEY_INSTALLED=%s" % keypath)
    log("ISSUER=%s" % cert.issuer.rfc4514_string())
    log("NOT_BEFORE=%s" % cert.not_valid_before_utc)
    log("NOT_AFTER=%s" % cert.not_valid_after_utc)
    log("SERIAL=%s" % cert.serial_number)

    if args.restart:
        log("")
        log("restarting caddy task MES-TLS-8900 ...")
        subprocess.run(["schtasks", "/end", "/tn", "MES-TLS-8900"], capture_output=True)
        time.sleep(2)
        subprocess.run(["schtasks", "/run", "/tn", "MES-TLS-8900"], capture_output=True)
        ok = False
        for _ in range(20):
            time.sleep(1.5)
            s = socket.socket()
            s.settimeout(2)
            try:
                s.connect(("127.0.0.1", 8900))
                ok = True
                break
            except Exception:
                pass
            finally:
                s.close()
        log("CADDY_8900_UP=%s" % ("YES" if ok else "NO"))


def mode_status(args):
    st = load_json(STATE_FILE)
    log("STATE_FILE=%s" % STATE_FILE)
    log(json.dumps(st, indent=2, ensure_ascii=False) if st else "none")
    cert_dir = os.path.join(CERT_ROOT, (st or {}).get("domain", args.domain))
    p = os.path.join(cert_dir, "fullchain.pem")
    if os.path.exists(p):
        c = x509.load_pem_x509_certificate(open(p, "rb").read())
        log("INSTALLED_ISSUER=%s" % c.issuer.rfc4514_string())
        log("INSTALLED_NOT_AFTER=%s" % c.not_valid_after_utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["new", "check", "finish", "status"])
    ap.add_argument("--domain", default="mes.example.com")
    ap.add_argument("--email", default="admin@example.com")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--restart", action="store_true")
    args = ap.parse_args()
    {"new": mode_new, "check": mode_check, "finish": mode_finish, "status": mode_status}[args.mode](args)


if __name__ == "__main__":
    main()
