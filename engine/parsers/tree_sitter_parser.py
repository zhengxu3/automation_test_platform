#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2026/5/20
@Python：python 3.9
@Description: Tree-sitter AST 解析器，支持 PHP/Go 等语言
"""
import os
from typing import List, Dict
from engine.parsers.base_parser import BaseParser


class TreeSitterParser(BaseParser):
    """基于 tree-sitter 的通用 AST 解析器"""

    # 各语言的节点类型映射
    LANG_CONFIG = {
        "php": {
            "module": "tree_sitter_php",
            "lang_func": "language_php",
            "extensions": [".php"],
            "node_types": {
                "class_declaration": "class",
                "interface_declaration": "interface",
                "trait_declaration": "trait",
                "method_declaration": "method",
                "function_definition": "function",
            },
            "skip_dirs": ["vendor", "storage", "bootstrap/cache", "public"],
        },
        "go": {
            "module": "tree_sitter_go",
            "lang_func": "language",
            "extensions": [".go"],
            "node_types": {
                "function_declaration": "function",
                "method_declaration": "method",
                "type_declaration": "type",
            },
            "skip_dirs": ["vendor"],
        },
    }

    def __init__(self):
        self._parsers = {}  # 缓存已初始化的 parser

    def _get_parser(self, lang: str):
        """懒加载 tree-sitter parser"""
        if lang in self._parsers:
            return self._parsers[lang]

        import tree_sitter

        lang_cfg = self.LANG_CONFIG.get(lang)
        if not lang_cfg:
            raise ValueError(f"TreeSitterParser 不支持语言: {lang}")

        import importlib
        mod = importlib.import_module(lang_cfg["module"])
        lang_func = getattr(mod, lang_cfg["lang_func"])
        language = tree_sitter.Language(lang_func())
        parser = tree_sitter.Parser(language)

        self._parsers[lang] = (parser, language, lang_cfg)
        return parser, language, lang_cfg

    async def parse(self, repo_path: str, lang: str, target_file: str = None) -> List[Dict]:
        parser, language, lang_cfg = self._get_parser(lang)
        extensions = lang_cfg["extensions"]
        skip_dirs = set(lang_cfg.get("skip_dirs", []))
        node_types = lang_cfg["node_types"]

        nodes = []

        if target_file:
            files = [target_file]
        else:
            files = self._collect_files(repo_path, extensions, skip_dirs)

        for file_path in files:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    source = f.read()
                if not source.strip():
                    continue

                tree = parser.parse(source.encode('utf-8'))
                rel_path = os.path.relpath(file_path, repo_path)
                file_nodes = self._extract_nodes(tree.root_node, source, rel_path, node_types, lang)
                nodes.extend(file_nodes)
            except Exception:
                continue

        return nodes

    def _collect_files(self, repo_path: str, extensions: list, skip_dirs: set) -> List[str]:
        """收集目标文件"""
        files = []
        for root, dirs, filenames in os.walk(repo_path):
            # 跳过隐藏目录和无关目录
            dirs[:] = [d for d in dirs if not d.startswith('.')
                       and d not in ('node_modules', '__pycache__', '.git')
                       and not any(os.path.relpath(os.path.join(root, d), repo_path).startswith(s) for s in skip_dirs)]
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext in extensions:
                    files.append(os.path.join(root, fname))
        return files

    def _extract_nodes(self, root_node, source: str, file_path: str, node_types: dict, lang: str) -> List[Dict]:
        """从 AST 中提取结构化节点"""
        nodes = []
        namespace = ""
        class_name = ""

        def visit(node, current_class=""):
            nonlocal namespace, class_name

            # PHP namespace 提取
            if lang == "php" and node.type == "namespace_definition":
                ns_name = node.child_by_field_name("name")
                if ns_name:
                    namespace = ns_name.text.decode('utf-8')

            # 类/接口/trait
            if node.type in node_types and node_types[node.type] in ("class", "interface", "trait"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = name_node.text.decode('utf-8')
                    code_content = source[node.start_byte:node.end_byte]
                    node_id = f"/{file_path}::{class_name}#L{node.start_point[0] + 1}"
                    nodes.append({
                        "node_id": node_id,
                        "node_type": node_types[node.type],
                        "name": class_name,
                        "namespace": namespace,
                        "file_path": "/" + file_path,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "code_content": code_content[:500],
                        "full_name": f"{namespace}\\{class_name}" if namespace else class_name,
                        "calls_out": self._extract_calls(node, source, lang),
                    })
                    # 递归提取类内方法
                    for child in node.children:
                        visit(child, class_name)
                    return

            # 方法/函数
            if node.type in node_types and node_types[node.type] in ("method", "function"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = name_node.text.decode('utf-8')
                    code_content = source[node.start_byte:node.end_byte]
                    node_id = f"/{file_path}::{current_class}::{func_name}#L{node.start_point[0] + 1}" if current_class else f"/{file_path}::{func_name}#L{node.start_point[0] + 1}"
                    nodes.append({
                        "node_id": node_id,
                        "node_type": node_types[node.type],
                        "name": func_name,
                        "class_name": current_class,
                        "namespace": namespace,
                        "file_path": "/" + file_path,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "code_content": code_content,
                        "full_name": f"{namespace}\\{current_class}::{func_name}" if current_class else func_name,
                        "calls_out": self._extract_calls(node, source, lang),
                    })
                return

            # Go type declaration
            if lang == "go" and node.type == "type_declaration":
                for child in node.children:
                    if child.type == "type_spec":
                        name_node = child.child_by_field_name("name")
                        if name_node:
                            type_name = name_node.text.decode('utf-8')
                            code_content = source[node.start_byte:node.end_byte]
                            nodes.append({
                                "node_id": f"/{file_path}::{type_name}#L{node.start_point[0] + 1}",
                                "node_type": "type",
                                "name": type_name,
                                "namespace": "",
                                "file_path": "/" + file_path,
                                "start_line": node.start_point[0] + 1,
                                "end_line": node.end_point[0] + 1,
                                "code_content": code_content[:500],
                                "full_name": type_name,
                                "calls_out": [],
                            })

            for child in node.children:
                visit(child, current_class)

        visit(root_node)
        return nodes

    def _extract_calls(self, node, source: str, lang: str) -> List[str]:
        """提取方法体内的函数/方法调用"""
        calls = set()

        def find_calls(n):
            # PHP: $this->method() / Class::method() / function()
            if lang == "php":
                if n.type == "member_call_expression":
                    name = n.child_by_field_name("name")
                    if name:
                        calls.add(name.text.decode('utf-8'))
                elif n.type == "scoped_call_expression":
                    name = n.child_by_field_name("name")
                    if name:
                        calls.add(name.text.decode('utf-8'))
                elif n.type == "function_call_expression":
                    func = n.child_by_field_name("function")
                    if func and func.type == "name":
                        calls.add(func.text.decode('utf-8'))
            # Go: function()
            elif lang == "go":
                if n.type == "call_expression":
                    func = n.child_by_field_name("function")
                    if func:
                        calls.add(func.text.decode('utf-8').split('.')[-1])

            for child in n.children:
                find_calls(child)

        find_calls(node)
        return list(calls)[:50]  # 限制最多50个调用
