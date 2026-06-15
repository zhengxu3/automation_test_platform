#!/usr/bin/env python3
"""真实后端缺陷验证：关 mock，让 LLM 生成 API 用例真打服务。

用途：
  python scripts/verify_real_api_bug.py

判定标准：
  - mock_mode=False；
  - 起一个真实 HTTP 后端；
  - LLM 生成的 requests 脚本真打 base_url；
  - cases_failed > 0 才算"抓到 bug"，脚本崩溃导致的 fail 不算。
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from device_worker.tasks.api_test_task import execute_api_test  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_buggy_service(repo_path: str, port: int):
    os.makedirs(repo_path, exist_ok=True)
    app = f'''
from flask import Flask, jsonify, request

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify({{"ok": True}})


@app.post("/login")
def login():
    data = request.get_json(silent=True) or {{}}
    phone = str(data.get("phone", ""))
    password = str(data.get("password", ""))
    if not password:
        return jsonify({{"ok": False, "code": "MISSING_PASSWORD", "msg": "缺少密码"}}), 400

    # BUG: 真实规则要求手机号必须严格 11 位；这里错误地接受了 10 位数字。
    if phone.isdigit() and len(phone) >= 10:
        return jsonify({{"ok": True, "code": "OK", "token": "demo-token"}}), 200

    return jsonify({{"ok": False, "code": "INVALID_PHONE", "msg": "手机号必须为11位"}}), 400


if __name__ == "__main__":
    app.run(host="127.0.0.1", port={port}, debug=False)
'''
    with open(os.path.join(repo_path, "app.py"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(app).lstrip())


def _wait_health(base_url: str, timeout: int = 10):
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.2)
    raise RuntimeError(f"后端启动超时: {last_error}")


def _parse_report(result: dict) -> dict:
    try:
        return json.loads(result.get("report") or "{}")
    except Exception:
        return {}


def run(model_id: str, out_path: str = "") -> dict:
    work_root = tempfile.mkdtemp(prefix="real_api_bug_")
    repo_path = os.path.join(work_root, "buggy_login_service")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    _write_buggy_service(repo_path, port)

    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_health(base_url)
        goal_id = f"goal_real_api_bug_{uuid.uuid4().hex[:8]}"
        logs = []
        result = execute_api_test(
            {
                "goal_id": goal_id,
                "req_id": "real_api_bug",
                "step_id": "s_api_real_bug",
                "task_id": f"gtask_{uuid.uuid4().hex[:8]}",
                "repo_path": repo_path,
                "base_url": base_url,
                "mock_mode": False,
                "affected_modules": ["app.py"],
                "change_summary": "登录接口手机号校验存在缺陷：真实业务要求手机号必须严格 11 位，但实现错误地接受了 10 位数字。",
                "requirement_context": (
                    "验证登录接口手机号校验。规则：phone 必须是 11 位数字。"
                    "当 phone 为 10 位数字(如 1380000000)时，接口必须失败，HTTP 400，"
                    "响应 JSON: ok=false, code=INVALID_PHONE。"
                    "当 phone 为 11 位数字且 password 非空时，接口可以成功。"
                ),
                "test_accounts": [{"phone": "13800000000", "password": "pass123"}],
                "interface_doc": {
                    "summary": "登录接口契约。重点验证 10 位手机号必须失败。",
                    "affected_endpoints": [
                        {
                            "method": "POST",
                            "path": "/login",
                            "impact": "direct",
                            "reason": "手机号校验逻辑直接位于 login handler。",
                            "request": {
                                "json": {
                                    "phone": "string, required, must be exactly 11 digits",
                                    "password": "string, required",
                                }
                            },
                            "responses": {
                                "success": {"status": 200, "json": {"ok": True, "code": "OK"}},
                                "invalid_phone": {
                                    "status": 400,
                                    "json": {"ok": False, "code": "INVALID_PHONE"},
                                },
                            },
                            "test_scenarios": [
                                {
                                    "name": "10_digit_phone_must_fail",
                                    "request": {"phone": "1380000000", "password": "pass123"},
                                    "expected_status": 400,
                                    "expected_json": {"ok": False, "code": "INVALID_PHONE"},
                                },
                                {
                                    "name": "11_digit_phone_can_pass",
                                    "request": {"phone": "13800000000", "password": "pass123"},
                                    "expected_status": 200,
                                    "expected_json": {"ok": True},
                                },
                            ],
                        }
                    ],
                },
            },
            db=None,
            model_id=model_id,
            log=lambda msg: logs.append(str(msg)),
        )
        report = _parse_report(result)
        cases_failed = int(result.get("cases_failed") or 0)
        caught_bug = result.get("test_result") == "fail" and cases_failed > 0
        verification = {
            "caught_bug": caught_bug,
            "mock_mode": False,
            "base_url": base_url,
            "repo_path": repo_path,
            "model_id": model_id,
            "result": result,
            "report": report,
            "logs": logs,
            "server_stdout": "",
            "server_stderr": "",
        }
        if out_path:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(verification, f, ensure_ascii=False, indent=2)
        return verification
    finally:
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate(timeout=3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini_flash")
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "real_api_bug_verification.json"))
    args = ap.parse_args()

    verification = run(args.model, args.out)
    print(json.dumps({
        "caught_bug": verification["caught_bug"],
        "mock_mode": verification["mock_mode"],
        "base_url": verification["base_url"],
        "model_id": verification["model_id"],
        "test_result": verification["result"].get("test_result"),
        "cases_passed": verification["result"].get("cases_passed"),
        "cases_failed": verification["result"].get("cases_failed"),
        "summary": verification["result"].get("summary"),
        "script_path": verification["result"].get("ref"),
        "report_path": args.out,
    }, ensure_ascii=False, indent=2))
    raise SystemExit(0 if verification["caught_bug"] else 1)


if __name__ == "__main__":
    main()
