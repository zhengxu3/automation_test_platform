#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2026/5/12
@Python：python 3.9
"""

from engine.parsers.base_parser import BaseParser
from engine.parsers.jar_parser import JvmJarParser
from engine.parsers.tree_sitter_parser import TreeSitterParser
from engine.parsers.swift_parser import SwiftASTParser


class ParserFactory:
    # 支持的语言列表（用于错误提示）
    SUPPORTED_LANGUAGES = ["android", "java", "go", "php", "ios"]

    @staticmethod
    def get_parser(parser_type: str) -> BaseParser:
        if parser_type == "jvm_jar":
            return JvmJarParser()
        elif parser_type == "tree_sitter":
            return TreeSitterParser()
        elif parser_type == "swift":
            return SwiftASTParser()
        else:
            raise ValueError(f"未知的解析器引擎类型: {parser_type}")
