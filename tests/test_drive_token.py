"""drive-token（GitHub Actions から暗号化トークンを受け取る仕組み）の模擬テスト。

ワークフロー（.github/workflows/drive-token.yml）に埋め込んだ Python をそのまま取り出して実行する。
外部への通信はしない（google モードは 127.0.0.1 の模擬サーバー）。費用なし。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from storyengine import drive_token as dt
from storyengine import google_drive
from storyengine.cli import main

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "drive-token.yml"


def issuer_script() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    m = re.search(r"\n( *)# --- BEGIN DRIVE TOKEN ISSUER.*?\n(.*?)\n *# --- END DRIVE TOKEN ISSUER ---", text, re.S)
    indent = len(m.group(1))
    return "\n".join(line[indent:] for line in m.group(2).splitlines())


def run_issuer(env: dict[str, str]) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "SYSTEMROOT")}
    return subprocess.run([sys.executable, "-I", "-c", issuer_script()], env={**base, **env},
                          capture_output=True, text=True, timeout=60)


def as_log(stdout: str) -> str:
    # GitHub のログのようにタイムスタンプを付ける
    return "\n".join(f"2026-11-23T05:00:0{i % 10}.0000000Z {line}" for i, line in enumerate(stdout.splitlines()))


class FakeGoogle(BaseHTTPRequestHandler):
    token = "ya29." + "A" * 1800  # 長いアクセストークン
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        FakeGoogle.seen.append(form)
        body = json.dumps({"access_token": self.token, "expires_in": 3599}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        ok = self.headers.get("Authorization") == f"Bearer {self.token}"
        self.send_response(200 if ok else 401)
        self.end_headers()
        if ok:
            self.wfile.write(json.dumps({"storageQuota": {"limit": str(15 * 1024 ** 3), "usage": "1024"}}).encode())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"STORYENGINE_TOKEN_TMP": str(self.tmp)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def key_dirs(self):
        d = self.tmp / "storyengine-drive-token"
        return list(d.iterdir()) if d.exists() else []

    def save_log(self, text: str) -> Path:
        p = self.tmp / "job.log"
        p.write_text(text, encoding="utf-8")
        return p


class TestDummyRoundTrip(Base):
    def test_dummy_long_token_roundtrip_and_cleanup(self):
        req = dt.begin("dummy")
        self.assertEqual(oct((self.tmp / "storyengine-drive-token" / req["request_id"]).stat().st_mode & 0o777), "0o700")
        proc = run_issuer({"REQUEST_ID": req["request_id"], "PUBLIC_KEY_B64": req["public_key"], "MODE": "dummy"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        log = self.save_log(as_log(proc.stdout))
        token, header = dt.finish(req["request_id"], log)
        self.assertTrue(token.startswith("dummy-"))
        self.assertGreater(len(token), 2000)  # RSA だけでは包めない長さ
        self.assertEqual(header["mode"], "dummy")
        self.assertEqual(self.key_dirs(), [])  # 秘密鍵を残さない
        self.assertFalse(log.exists())  # ログファイルも残さない

    def test_cli_begin_finish_does_not_print_token(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["drive-token", "begin", "--mode", "dummy"]), 0)
        req = json.loads(buf.getvalue())
        self.assertEqual(req["workflow"], "drive-token.yml")
        self.assertEqual(req["run_name"], f"drive-token {req['request_id']}")
        proc = run_issuer({"REQUEST_ID": req["request_id"], "PUBLIC_KEY_B64": req["public_key"], "MODE": "dummy"})
        log = self.save_log(as_log(proc.stdout))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["drive-token", "finish", "--request-id", req["request_id"], "--log", str(log)]), 0)
        out = buf.getvalue()
        self.assertIn("復号成功", out)
        self.assertNotIn("dummy-", out)
        self.assertEqual(self.key_dirs(), [])


class TestGoogleMode(Base):
    def test_google_mode_with_local_fake(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeGoogle)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}"
            req = dt.begin("google")
            proc = run_issuer({"REQUEST_ID": req["request_id"], "PUBLIC_KEY_B64": req["public_key"], "MODE": "google",
                               "GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "csec",
                               "GOOGLE_DRIVE_REFRESH_TOKEN": "rt",
                               "DRIVE_TOKEN_URL": url + "/token", "DRIVE_ABOUT_URL": url + "/about"})
        finally:
            srv.shutdown()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(FakeGoogle.seen[-1]["refresh_token"], ["rt"])
        # 平文のトークンが出るのは伏せ字指定の行だけ（GitHub はこの行を表示せず、以後のログでも伏せる）
        lines = [l for l in proc.stdout.splitlines() if FakeGoogle.token in l]
        self.assertEqual(lines, [f"::add-mask::{FakeGoogle.token}"])
        self.assertNotIn("rt", [w for w in proc.stdout.split()])
        token, header = dt.finish(req["request_id"], self.save_log(as_log(proc.stdout)))
        self.assertEqual(token, FakeGoogle.token)
        self.assertEqual(header["quota"]["limit"], str(15 * 1024 ** 3))

    def test_google_mode_requires_secrets(self):
        req = dt.begin("google")
        proc = run_issuer({"REQUEST_ID": req["request_id"], "PUBLIC_KEY_B64": req["public_key"], "MODE": "google"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("DRIVE_TOKEN_ERROR google secrets are not configured", proc.stdout)
        dt.cleanup_all()

    def test_only_loopback_overrides_are_honored(self):
        req = dt.begin("google")
        proc = run_issuer({"REQUEST_ID": req["request_id"], "PUBLIC_KEY_B64": req["public_key"], "MODE": "google",
                           "GOOGLE_OAUTH_CLIENT_ID": "c", "GOOGLE_OAUTH_CLIENT_SECRET": "s",
                           "GOOGLE_DRIVE_REFRESH_TOKEN": "r", "DRIVE_TOKEN_URL": "http://evil.example/token"})
        # 127.0.0.1 以外の差し替えは無視され、本物の Google（この環境では接続不可またはエラー）へ向かう
        self.assertNotIn("evil.example", proc.stdout + proc.stderr)
        dt.cleanup_all()


class TestTamperAndBinding(Base):
    def make(self, mode="dummy"):
        req = dt.begin(mode)
        proc = run_issuer({"REQUEST_ID": req["request_id"], "PUBLIC_KEY_B64": req["public_key"], "MODE": mode})
        env_b64 = dt.find_envelope(proc.stdout, req["request_id"])
        key = self.tmp / "storyengine-drive-token" / req["request_id"] / "key.pem"
        return req, env_b64, key

    def test_any_modified_field_is_rejected(self):
        req, env_b64, key = self.make()
        dt.open_envelope(env_b64, req["request_id"], key)  # 改変前は通る
        env = json.loads(base64.b64decode(env_b64))
        for field in ("h", "k", "iv", "ct", "mac"):
            raw = bytearray(base64.b64decode(env[field]))
            raw[len(raw) // 2] ^= 0x01
            bad = dict(env, **{field: base64.b64encode(bytes(raw)).decode()})
            with self.subTest(field), self.assertRaises(dt.TokenError):
                dt.open_envelope(base64.b64encode(json.dumps(bad).encode()).decode(), req["request_id"], key)

    def test_truncated_or_garbage_is_rejected(self):
        req, env_b64, key = self.make()
        for bad in (env_b64[:-20], "!!!notbase64", base64.b64encode(b"{}").decode()):
            with self.subTest(bad[:10]), self.assertRaises(dt.TokenError):
                dt.open_envelope(bad, req["request_id"], key)

    def test_result_for_another_request_is_rejected(self):
        a, env_a, _ = self.make()
        b, _, key_b = self.make()
        with self.assertRaises(dt.TokenError):  # B の鍵では A の結果を開けない
            dt.open_envelope(env_a, b["request_id"], key_b)
        log = self.save_log(f"{dt.PREFIX} {b['request_id']} {env_a}")  # A の結果を B の行として差し込む
        with self.assertRaises(dt.TokenError):
            dt.finish(b["request_id"], log)
        self.assertFalse((self.tmp / "storyengine-drive-token" / b["request_id"]).exists())  # 失敗しても鍵を消す
        self.assertFalse(log.exists())

    def test_header_request_id_mismatch_detected_with_valid_mac(self):
        req, env_b64, key = self.make()
        other = "f" * 32
        with self.assertRaises(dt.TokenError):
            dt.open_envelope(env_b64, other, key)

    def test_expired_and_duplicate(self):
        req, env_b64, key = self.make()
        with self.assertRaises(dt.TokenError):
            dt.open_envelope(env_b64, req["request_id"], key, now=time.time() + 16 * 60)
        line = f"{dt.PREFIX} {req['request_id']} {env_b64}"
        with self.assertRaises(dt.TokenError):
            dt.find_envelope(line + "\n" + line, req["request_id"])

    def test_mode_mismatch_rejected(self):
        req = dt.begin("google")
        proc = run_issuer({"REQUEST_ID": req["request_id"], "PUBLIC_KEY_B64": req["public_key"], "MODE": "dummy"})
        with self.assertRaises(dt.TokenError):
            dt.finish(req["request_id"], self.save_log(proc.stdout))

    def test_stale_keys_are_swept(self):
        req = dt.begin("dummy")
        self.assertEqual(dt.sweep_stale(now=time.time() + dt.KEY_TTL_SEC + 1), 1)
        self.assertEqual(self.key_dirs(), [])


class TestIssuerInputValidation(Base):
    def test_rejects_bad_inputs_without_shell_execution(self):
        marker = self.tmp / "pwned"
        good = dt.begin("dummy")
        weak = subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
                              capture_output=True, check=True).stdout
        weak_pub = subprocess.run(["openssl", "pkey", "-pubout"], input=weak, capture_output=True, check=True).stdout
        cases = {
            "request_id": {"REQUEST_ID": f"$(touch {marker})", "PUBLIC_KEY_B64": good["public_key"], "MODE": "dummy"},
            "mode": {"REQUEST_ID": good["request_id"], "PUBLIC_KEY_B64": good["public_key"], "MODE": "google;id"},
            "not_base64": {"REQUEST_ID": good["request_id"], "PUBLIC_KEY_B64": f"`touch {marker}`", "MODE": "dummy"},
            "not_pem": {"REQUEST_ID": good["request_id"],
                        "PUBLIC_KEY_B64": base64.b64encode(f"$(touch {marker})".encode()).decode(), "MODE": "dummy"},
            "weak_rsa": {"REQUEST_ID": good["request_id"], "PUBLIC_KEY_B64": base64.b64encode(weak_pub).decode(),
                         "MODE": "dummy"},
        }
        for name, env in cases.items():
            with self.subTest(name):
                proc = run_issuer(env)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("DRIVE_TOKEN_ERROR", proc.stdout)
                self.assertNotIn(dt.PREFIX, proc.stdout)
        self.assertFalse(marker.exists())
        dt.cleanup_all()


class TestWorkflowStatic(unittest.TestCase):
    text = WORKFLOW.read_text(encoding="utf-8")

    def test_minimal_permissions_and_triggers(self):
        self.assertIn("\npermissions: {}\n", self.text)
        self.assertIn("    permissions: {}\n", self.text)  # ジョブ単位でも権限なし
        self.assertRegex(self.text, r"\non:\n  workflow_dispatch:\n")
        self.assertNotIn("pull_request", self.text)
        self.assertNotIn("uses:", self.text)  # 外部アクションもチェックアウトも使わない
        self.assertIn("timeout-minutes:", self.text)

    def test_no_expressions_inside_run_script(self):
        run_block = self.text.split("        run: |", 1)[1]
        self.assertNotIn("${{", run_block)

    def test_secrets_only_for_google_mode(self):
        refs = re.findall(r"secrets\.[A-Z_]+", self.text)
        self.assertEqual(len(refs), 3)
        for line in self.text.splitlines():
            if re.search(r"secrets\.[A-Z_]", line):
                self.assertIn("inputs.mode == 'google' &&", line)


class TestCliThen(Base):
    def test_then_is_parsed_after_options(self):
        from storyengine import cli
        captured = {}
        real_finish = dt.finish

        def fake_finish(rid, log):
            captured["args"] = (rid, str(log))
            return "tok", {"mode": "google", "issued_at": int(time.time()), "expires_in": 3600, "quota": None}

        calls = []
        real_main = cli.main

        def spy_main(argv=None):
            if argv and argv[0] == "--series":
                calls.append((argv[2:], google_drive._SESSION_TOKEN.get("token")))
                return 0
            return real_main(argv)

        with mock.patch.object(dt, "finish", fake_finish), mock.patch.object(cli, "main", spy_main):
            with redirect_stdout(io.StringIO()):
                code = real_main(["drive-token", "finish", "--request-id", "a" * 32, "--log", "x.log",
                                  "--then", "persist", "--episode", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(captured["args"], ("a" * 32, "x.log"))
        self.assertEqual(calls, [(["persist", "--episode", "1"], "tok")])
        self.assertEqual(google_drive._SESSION_TOKEN, {})  # 実行後にメモリから消す


class TestSessionTokenInStore(unittest.TestCase):
    def tearDown(self):
        google_drive.clear_session_token()

    def test_session_token_used_and_expiry_enforced(self):
        store = google_drive.GoogleDriveStore({"provider": "google-drive", "params": {}})
        with mock.patch.dict(os.environ, {}, clear=True):
            google_drive.set_session_token("tok-xyz", time.time() + 600)
            self.assertEqual(store._access_token(), "tok-xyz")
            google_drive.set_session_token("tok-old", time.time() - 1)
            with self.assertRaises(google_drive.DriveError):
                store._access_token()
            google_drive.clear_session_token()
            self.assertEqual(store._access_token(), google_drive.PLACEHOLDER_BEARER)


if __name__ == "__main__":
    unittest.main()
