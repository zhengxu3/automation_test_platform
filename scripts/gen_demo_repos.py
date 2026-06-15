"""生成演示用模拟代码库（确定性脚手架）：

  1. demo-shop        混合前后端单仓（root: app.py+requirements.txt+package.json+vite.config.ts）
  2. demo-order-api   纯后端
  3. demo-shop-web    纯 web 前端

每个 git init + 首次提交。可选 --register 把 repo_id→local_path 写入 ai_git_repos，
这样页面建任务"只填库地址"也能反查到本地（不重 clone）。

用法：
  python scripts/gen_demo_repos.py [--base ~/Documents/work_code] [--register]
脚本最后打印三种建任务的 sources 配置（含 role / env / mock 开关）。
"""
import argparse
import json
import os
import subprocess


def _w(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------- 文件内容模板 ----------
FLASK_APP = '''"""演示后端：商城订单服务（Flask）。改这里的路由/校验 = 后端爆炸范围。"""
from flask import Flask, request, jsonify

app = Flask(__name__)


@app.route("/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    phone = data.get("phone", "")
    if not phone or len(phone) != 11:
        return jsonify({"code": "INVALID_PHONE", "ok": False}), 400
    return jsonify({"ok": True, "token": "demo-token"})


@app.route("/orders", methods=["GET"])
def list_orders():
    return jsonify({"ok": True, "orders": []})


@app.route("/orders", methods=["POST"])
def create_order():
    data = request.get_json() or {}
    if not data.get("product_id"):
        return jsonify({"code": "MISSING_PRODUCT", "ok": False}), 400
    return jsonify({"ok": True, "order_id": "od_1001"})


if __name__ == "__main__":
    app.run(port=8080)
'''

BACKEND_ROUTES = '''"""后端订单领域逻辑（演示用）。改这里 = 后端爆炸范围 → 触发 api 测试。"""


def calc_total(items):
    return sum(i.get("price", 0) * i.get("qty", 1) for i in items)


def validate_order(payload):
    if not payload.get("product_id"):
        return False, "MISSING_PRODUCT"
    if payload.get("qty", 1) <= 0:
        return False, "INVALID_QTY"
    return True, ""
'''

REQUIREMENTS = "flask>=3.0\nrequests>=2.31\n"

PACKAGE_JSON = json.dumps({
    "name": "demo-shop-web",
    "private": True,
    "version": "0.1.0",
    "scripts": {"dev": "vite", "build": "vite build"},
    "dependencies": {"vue": "^3.4.0", "vue-router": "^4.4.0"},
    "devDependencies": {"vite": "^5.4.0", "@vitejs/plugin-vue": "^5.1.0"},
}, indent=2, ensure_ascii=False) + "\n"

VITE_CONFIG = '''import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"

export default defineConfig({ plugins: [vue()], server: { port: 3001 } })
'''

LOGIN_VUE = '''<template>
  <!-- 演示 web 前端：登录页。改这里 = web 爆炸范围 → 触发 web ui 测试。 -->
  <div class="login">
    <input v-model="phone" placeholder="手机号" />
    <button @click="submit">登录</button>
    <p v-if="error" class="err">{{ error }}</p>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue"
const phone = ref("")
const error = ref("")
function submit() {
  if (phone.value.length !== 11) { error.value = "手机号格式错误"; return }
  error.value = ""
}
</script>
'''

ORDERS_VUE = '''<template>
  <div class="orders">
    <h2>我的订单</h2>
    <ul><li v-for="o in orders" :key="o.id">{{ o.title }}</li></ul>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue"
const orders = ref<any[]>([])
</script>
'''

GRADLE = "android {\n    compileSdk 34\n    defaultConfig { applicationId 'com.demo.shop' }\n}\ndependencies {}\n"
SETTINGS_GRADLE = "include ':app'\n"
ANDROID_MANIFEST = '<?xml version="1.0"?>\n<manifest package="com.demo.shop">\n  <application android:label="DemoShop"/>\n</manifest>\n'
KOTLIN_LOGIN = '''package com.demo.shop

// 演示客户端(Android)：登录页。改这里 = 客户端爆炸范围 → 触发 device_test(真机 UI)。
class LoginActivity {
    fun validatePhone(phone: String): Boolean {
        return phone.length == 11
    }

    fun onLogin(phone: String) {
        if (!validatePhone(phone)) {
            showError("手机号格式错误")
            return
        }
        submit(phone)
    }

    private fun showError(msg: String) {}
    private fun submit(phone: String) {}
}
'''

PODFILE = "platform :ios, '15.0'\ntarget 'DemoShop' do\n  use_frameworks!\nend\n"
IOS_PLIST = '<?xml version="1.0"?>\n<plist version="1.0"><dict>\n  <key>CFBundleName</key><string>DemoShop</string>\n</dict></plist>\n'
SWIFT_LOGIN = '''import UIKit

// 演示客户端(iOS)：登录页。改这里 = 客户端爆炸范围 → 触发 device_test(真机 UI)。
class LoginViewController: UIViewController {
    func validatePhone(_ phone: String) -> Bool {
        return phone.count == 11
    }

    func onLogin(_ phone: String) {
        if !validatePhone(phone) {
            showError("手机号格式错误")
            return
        }
        submit(phone)
    }

    private func showError(_ msg: String) {}
    private func submit(_ phone: String) {}
}
'''


def _git_init(path):
    env = ["git", "-c", "user.email=demo@local", "-c", "user.name=demo"]
    subprocess.run(["git", "init", "-q"], cwd=path, capture_output=True)
    subprocess.run(env + ["add", "-A"], cwd=path, capture_output=True)
    subprocess.run(env + ["commit", "-q", "-m", "chore: 初始化演示仓库"], cwd=path, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=path,
                          capture_output=True, text=True).stdout.strip()
    return head


def build(base):
    repos = {}

    # 1) 混合前后端单仓：root 同时有后端与 web 签名文件 → inspect 识别 backend+web
    mixed = os.path.join(base, "demo-shop")
    _w(os.path.join(mixed, "app.py"), FLASK_APP)
    _w(os.path.join(mixed, "requirements.txt"), REQUIREMENTS)
    _w(os.path.join(mixed, "package.json"), PACKAGE_JSON)
    _w(os.path.join(mixed, "vite.config.ts"), VITE_CONFIG)
    _w(os.path.join(mixed, "backend", "routes.py"), BACKEND_ROUTES)
    _w(os.path.join(mixed, "web", "src", "views", "Login.vue"), LOGIN_VUE)
    _w(os.path.join(mixed, "web", "src", "views", "Orders.vue"), ORDERS_VUE)
    _w(os.path.join(mixed, "README.md"), "# demo-shop 混合前后端演示仓\n")
    repos["demo-shop"] = (mixed, "fullstack")

    # 2) 纯后端
    api = os.path.join(base, "demo-order-api")
    _w(os.path.join(api, "app.py"), FLASK_APP)
    _w(os.path.join(api, "requirements.txt"), REQUIREMENTS)
    _w(os.path.join(api, "backend", "routes.py"), BACKEND_ROUTES)
    _w(os.path.join(api, "README.md"), "# demo-order-api 纯后端演示仓\n")
    repos["demo-order-api"] = (api, "backend")

    # 3) 纯 web
    web = os.path.join(base, "demo-shop-web")
    _w(os.path.join(web, "package.json"), PACKAGE_JSON)
    _w(os.path.join(web, "vite.config.ts"), VITE_CONFIG)
    _w(os.path.join(web, "src", "views", "Login.vue"), LOGIN_VUE)
    _w(os.path.join(web, "src", "views", "Orders.vue"), ORDERS_VUE)
    _w(os.path.join(web, "README.md"), "# demo-shop-web 纯 web 演示仓\n")
    repos["demo-shop-web"] = (web, "web")

    # 4) 安卓客户端
    android = os.path.join(base, "demo-shop-android")
    _w(os.path.join(android, "build.gradle"), GRADLE)
    _w(os.path.join(android, "settings.gradle"), SETTINGS_GRADLE)
    _w(os.path.join(android, "app", "src", "main", "AndroidManifest.xml"), ANDROID_MANIFEST)
    _w(os.path.join(android, "app", "src", "main", "java", "com", "demo", "shop", "LoginActivity.kt"), KOTLIN_LOGIN)
    _w(os.path.join(android, "README.md"), "# demo-shop-android 安卓客户端演示仓\n")
    repos["demo-shop-android"] = (android, "android_client")

    # 5) iOS 客户端
    ios = os.path.join(base, "demo-shop-ios")
    _w(os.path.join(ios, "Podfile"), PODFILE)
    _w(os.path.join(ios, "Info.plist"), IOS_PLIST)
    _w(os.path.join(ios, "DemoShop", "LoginViewController.swift"), SWIFT_LOGIN)
    _w(os.path.join(ios, "README.md"), "# demo-shop-ios iOS 客户端演示仓\n")
    repos["demo-shop-ios"] = (ios, "ios")

    out = {}
    for repo_id, (path, role) in repos.items():
        head = _git_init(path)
        out[repo_id] = {"local_path": path, "role": role, "head": head}
    return out


def maybe_register(out):
    try:
        sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import sys
        sys.path.insert(0, sys_path)
        from common.db import get_collection
        col = get_collection("ai_git_repos")
        for repo_id, info in out.items():
            col.update_one({"repo_id": repo_id},
                           {"$set": {"repo_id": repo_id, "repo_name": repo_id,
                                     "git_url": f"local://{repo_id}",
                                     "local_path": info["local_path"], "branch": "main"}},
                           upsert=True)
        print("✅ 已注册到 ai_git_repos（页面只填库地址即可反查本地）")
    except Exception as e:
        print(f"⚠️ 注册 ai_git_repos 跳过（无 DB 连接？）：{e}")


def print_task_configs(out):
    base_url = "http://127.0.0.1:8080"   # 后端测试环境（mock 模式可随意填）
    web_url = "http://127.0.0.1:3001"
    doc = "实现商城登录与下单：手机号校验、订单创建错误码、登录页前端交互。"

    def repo_src(repo_id, role):
        return {"type": "repo", "repo_id": repo_id, "local_path": out[repo_id]["local_path"],
                "branch": "main", "role": role}

    env = {"type": "environment", "base_url": base_url, "web_url": web_url,
           "test_accounts": [{"phone": "13800000000"}],
           "apk_source": "mock://demo.apk", "device_profile": {"platform": "android", "mock": True},
           "api_test_mock": True, "web_test_mock": True, "device_test_mock": True,
           "mock_fail_rounds": 0}   # 成败由提交的代码(MOCK_BUG)决定，基线设 0；apk/device 为 mock 占位让 device_test 可达

    configs = {
        "A_混合前后端单仓（你主要演示这个）": {
            "title": "商城混合仓多目标验证",
            "completion_policy": "continuous",
            "auto_replan": False,    # 外部驱动：失败停 partial 等下次触发，一次改动=一轮
            "budget": {"max_replans": 6},
            "sources": [{"type": "doc", "content": doc}, repo_src("demo-shop", "fullstack"), env],
        },
        "B_纯后端": {
            "title": "订单接口验证",
            "completion_policy": "continuous",
            "auto_replan": False,
            "budget": {"max_replans": 6},
            "sources": [{"type": "doc", "content": doc}, repo_src("demo-order-api", "backend"), env],
        },
        "C_纯web": {
            "title": "商城 web 前端验证",
            "completion_policy": "continuous",
            "auto_replan": False,
            "budget": {"max_replans": 6},
            "sources": [{"type": "doc", "content": doc}, repo_src("demo-shop-web", "web"), env],
        },
        "D_安卓客户端": {
            "title": "商城安卓客户端验证",
            "completion_policy": "continuous",
            "auto_replan": False,
            "budget": {"max_replans": 6},
            "sources": [{"type": "doc", "content": doc}, repo_src("demo-shop-android", "android_client"), env],
        },
        "E_iOS客户端": {
            "title": "商城 iOS 客户端验证",
            "completion_policy": "continuous",
            "auto_replan": False,
            "budget": {"max_replans": 6},
            "sources": [{"type": "doc", "content": doc}, repo_src("demo-shop-ios", "ios"), env],
        },
        "F_一个任务多库监控（后端+web+安卓+iOS 各自测试）": {
            "title": "商城全栈多库验证",
            "completion_policy": "continuous",
            "auto_replan": False,
            "budget": {"max_replans": 8},
            "sources": [
                {"type": "doc", "content": doc},
                repo_src("demo-order-api", "backend"),
                repo_src("demo-shop-web", "web"),
                repo_src("demo-shop-android", "android_client"),
                repo_src("demo-shop-ios", "ios"),
                env,
            ],
        },
    }
    print("\n================ 建任务 sources 配置（POST /ai/goal/create 或页面）================")
    for name, cfg in configs.items():
        print(f"\n----- {name} -----")
        print(json.dumps(cfg, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.path.expanduser("~/Documents/work_code"))
    ap.add_argument("--register", action="store_true", help="把 repo_id→local_path 写入 ai_git_repos")
    args = ap.parse_args()

    out = build(args.base)
    print("✅ 生成完成：")
    for repo_id, info in out.items():
        print(f"  {repo_id:16} {info['role']:9} HEAD={info['head']}  {info['local_path']}")
    if args.register:
        maybe_register(out)
    print_task_configs(out)


if __name__ == "__main__":
    main()
