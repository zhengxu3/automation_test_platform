#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2026/5/12
@Python：python 3.9
"""

import os
import json
import time
import asyncio
from typing import List, Dict
from engine.parsers.base_parser import BaseParser


class JvmJarParser(BaseParser):
    def __init__(self):
        self.jar_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../bin/universal-extractor.jar"))

    async def parse(self, repo_path: str, lang: str, target_file: str = None) -> List[Dict]:
        output_json = f"/tmp/ast_result_{int(time.time() * 1000)}.json"
        jar_lang = "kotlin" if lang == "android" else "java"

        cmd = ["java", "-jar", self.jar_path, jar_lang, repo_path, output_json]
        if target_file:
            cmd.append(target_file)

        process = await asyncio.create_subprocess_exec(*cmd)
        await process.wait()

        if process.returncode != 0:
            raise Exception(f"Jar 解析引擎崩溃，退出码: {process.returncode}")

        if not os.path.exists(output_json):
            return []

        with open(output_json, 'r', encoding='utf-8') as f:
            nodes = json.load(f)

        os.remove(output_json)
        return nodes
