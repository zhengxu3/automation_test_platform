#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2026/5/12
@Python：python 3.9
"""

from typing import List, Dict


class BaseParser:
    """所有语言解析器的抽象基类"""
    async def parse(self, repo_path: str, lang: str, target_file: str = None) -> List[Dict]:
        raise NotImplementedError
