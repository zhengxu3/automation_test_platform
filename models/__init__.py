#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/19
@Python：python 3.9
"""

from flask_mongoengine import MongoEngine

db = MongoEngine()

from .callback_m import Callback
from .task_m import TaskM