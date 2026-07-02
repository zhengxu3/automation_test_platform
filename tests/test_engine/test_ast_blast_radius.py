"""Tests for engine/ast_blast_radius.py — tree-sitter 方法级 diff 精确定位受影响接口。"""
import pytest
from engine.ast_blast_radius import (
    extract_methods,
    methods_affected_by_diff,
    _parse_unified_diff_lines,
    _parse_laravel_routes,
    _detect_language,
    analyze_repo_diff,
    ASTDiffResult,
)


# ─── Language detection ──────────────────────────────────────────────────────

class TestDetectLanguage:
    def test_python(self):
        assert _detect_language("app/routes.py") == "python"

    def test_php(self):
        assert _detect_language("app/Http/Controllers/UserController.php") == "php"

    def test_java(self):
        assert _detect_language("src/main/java/UserController.java") == "java"

    def test_kotlin(self):
        assert _detect_language("src/main/kotlin/UserController.kt") == "kotlin"

    def test_unknown(self):
        assert _detect_language("README.md") is None
        assert _detect_language("style.css") is None


# ─── Python/Flask extraction ─────────────────────────────────────────────────

class TestPythonExtraction:
    FLASK_CODE = b"""
from flask import Blueprint
bp = Blueprint('auth', __name__)

@bp.post('/login')
def login():
    phone = request.json.get('phone')
    return {'ok': True}

@bp.route('/users', methods=['GET'])
def list_users():
    return []

@bp.put('/users/<uid>')
def update_user(uid):
    return {'updated': True}

def _helper():
    pass
"""

    def test_extracts_all_methods(self):
        methods = extract_methods(self.FLASK_CODE, "python")
        names = [m.name for m in methods]
        assert "login" in names
        assert "list_users" in names
        assert "update_user" in names
        assert "_helper" in names

    def test_route_annotations(self):
        methods = extract_methods(self.FLASK_CODE, "python")
        by_name = {m.name: m for m in methods}
        assert by_name["login"].route_method == "POST"
        assert by_name["login"].route_path == "/login"
        assert by_name["list_users"].route_method == "GET"
        assert by_name["list_users"].route_path == "/users"
        assert by_name["update_user"].route_method == "PUT"
        assert by_name["update_user"].route_path == "/users/<uid>"
        assert by_name["_helper"].route_path == ""

    def test_affected_by_diff(self):
        methods = extract_methods(self.FLASK_CODE, "python")
        login_m = next(m for m in methods if m.name == "login")
        # 模拟改了 login 函数体内的行
        affected = methods_affected_by_diff(
            self.FLASK_CODE, "python",
            {login_m.start_line + 1}  # 函数体内某行
        )
        assert len(affected) == 1
        assert affected[0].name == "login"
        assert affected[0].route_path == "/login"

    def test_no_affected_when_helper_changed(self):
        methods = extract_methods(self.FLASK_CODE, "python")
        helper_m = next(m for m in methods if m.name == "_helper")
        affected = methods_affected_by_diff(
            self.FLASK_CODE, "python",
            {helper_m.start_line}
        )
        assert len(affected) == 1
        assert affected[0].name == "_helper"
        assert affected[0].route_path == ""  # helper 不绑路由


# ─── Java/Spring extraction ──────────────────────────────────────────────────

class TestJavaExtraction:
    SPRING_CODE = b"""
package com.example;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/users")
public class UserController {
    @GetMapping("/list")
    public List<User> getUsers() {
        return userService.findAll();
    }

    @PostMapping("/create")
    public User createUser(@RequestBody UserDto dto) {
        return userService.create(dto);
    }

    private void helper() {}
}
"""

    def test_extracts_methods(self):
        methods = extract_methods(self.SPRING_CODE, "java")
        names = [m.name for m in methods]
        assert "getUsers" in names
        assert "createUser" in names
        assert "helper" in names

    def test_spring_annotations(self):
        methods = extract_methods(self.SPRING_CODE, "java")
        by_name = {m.name: m for m in methods}
        assert by_name["getUsers"].route_method == "GET"
        assert by_name["getUsers"].route_path == "/list"
        assert by_name["createUser"].route_method == "POST"
        assert by_name["createUser"].route_path == "/create"
        assert by_name["helper"].route_path == ""

    def test_affected_by_diff(self):
        methods = extract_methods(self.SPRING_CODE, "java")
        create_m = next(m for m in methods if m.name == "createUser")
        affected = methods_affected_by_diff(
            self.SPRING_CODE, "java",
            {create_m.start_line + 1}
        )
        assert any(m.name == "createUser" for m in affected)
        assert not any(m.name == "getUsers" for m in affected)


# ─── PHP extraction ──────────────────────────────────────────────────────────

class TestPHPExtraction:
    PHP_CODE = b"""<?php
namespace App\\Http\\Controllers;

class UserController extends Controller {
    public function login(Request $request) {
        return response()->json(['ok' => true]);
    }

    public function register(Request $request) {
        return response()->json(['created' => true]);
    }

    private function validate($data) {}
}
"""

    def test_extracts_methods(self):
        methods = extract_methods(self.PHP_CODE, "php")
        names = [m.name for m in methods]
        assert "login" in names
        assert "register" in names
        assert "validate" in names

    def test_class_name(self):
        methods = extract_methods(self.PHP_CODE, "php")
        for m in methods:
            assert m.class_name == "UserController"

    def test_affected_by_diff(self):
        methods = extract_methods(self.PHP_CODE, "php")
        login_m = next(m for m in methods if m.name == "login")
        affected = methods_affected_by_diff(
            self.PHP_CODE, "php",
            {login_m.start_line, login_m.start_line + 1}
        )
        assert len(affected) == 1
        assert affected[0].name == "login"


# ─── Laravel route parsing ───────────────────────────────────────────────────

class TestLaravelRoutes:
    def test_parse_routes(self, tmp_path):
        routes_dir = tmp_path / "routes"
        routes_dir.mkdir()
        (routes_dir / "api.php").write_text("""<?php
Route::post('/login', [UserController::class, 'login']);
Route::get('/users', [UserController::class, 'index']);
Route::put('/users/{id}', 'UserController@update');
""")
        result = _parse_laravel_routes(str(tmp_path))
        assert result.get("UserController@login") == ("POST", "/login")
        assert result.get("UserController@index") == ("GET", "/users")
        assert result.get("UserController@update") == ("PUT", "/users/{id}")


# ─── Unified diff line parsing ───────────────────────────────────────────────

class TestDiffParsing:
    def test_single_hunk(self):
        diff = "@@ -5,3 +5,4 @@\n+new line\n"
        lines = _parse_unified_diff_lines(diff)
        assert lines == {5, 6, 7, 8}

    def test_multiple_hunks(self):
        diff = "@@ -1,2 +1,3 @@\n+a\n@@ -10,0 +11,2 @@\n+b\n+c\n"
        lines = _parse_unified_diff_lines(diff)
        assert 1 in lines and 2 in lines and 3 in lines
        assert 11 in lines and 12 in lines

    def test_single_line_change(self):
        diff = "@@ -5,1 +5,1 @@\n-old\n+new\n"
        lines = _parse_unified_diff_lines(diff)
        assert lines == {5}


# ─── Integration: analyze_repo_diff with real git ────────────────────────────

class TestAnalyzeRepoDiff:
    def test_non_git_repo_returns_empty(self, tmp_path):
        result = analyze_repo_diff(str(tmp_path), ["app.py"])
        assert result.changed_methods == []
        assert result.affected_endpoints == []

    def test_flask_repo(self, tmp_path):
        """模拟一个 Flask 仓库，做一次提交后修改一个方法，验证 AST 精确定位。"""
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=str(repo), capture_output=True)

        # 初始提交
        app_py = repo / "app.py"
        app_py.write_text("""from flask import Flask
app = Flask(__name__)

@app.post('/login')
def login():
    return {'ok': True}

@app.get('/users')
def list_users():
    return []
""")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

        # 修改 login 函数
        app_py.write_text("""from flask import Flask
app = Flask(__name__)

@app.post('/login')
def login():
    phone = request.json.get('phone')
    if len(phone) != 11:
        return {'error': 'INVALID_PHONE'}, 400
    return {'ok': True}

@app.get('/users')
def list_users():
    return []
""")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "fix login"], cwd=str(repo), capture_output=True)

        # 运行 AST 分析
        result = analyze_repo_diff(str(repo), ["app.py"], "HEAD~1", "HEAD")
        assert result.files_analyzed == 1
        assert len(result.changed_methods) >= 1
        assert any(m.name == "login" for m in result.changed_methods)
        # login 绑了路由 → 应该出现在 affected_endpoints
        assert any(ep.path == "/login" for ep in result.affected_endpoints)
        # list_users 没改 → 不应该在里面
        assert not any(ep.path == "/users" for ep in result.affected_endpoints)


# ─── blast_radius.ast_changed_endpoints wrapper ──────────────────────────────

class TestBlastRadiusWrapper:
    def test_wrapper_returns_list(self, tmp_path):
        from engine.blast_radius import ast_changed_endpoints
        result = ast_changed_endpoints(str(tmp_path), ["x.py"])
        assert result == []
