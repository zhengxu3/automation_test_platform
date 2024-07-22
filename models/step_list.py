#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/22
@Python：python 3.9
"""
from mongoengine import *
import datetime
from . import db
from bson import ObjectId


class StepList(db.Document):
    task_id = StringField()
    create_time = DateTimeField(default=datetime.datetime.now)
    step_name = StringField()

    meta = {
        'collection': 'step_list',
        'ordering': ['create_time']  # 默认按时间正序排序
    }

    @classmethod
    def create_step(cls, task_id, step_name):
        step = cls(task_id=task_id, step_name=step_name)
        step.save()
        return step.id

    @classmethod
    def get_steps_by_task_id(cls, task_id):
        return cls.objects(task_id=task_id).order_by('create_time')

    @classmethod
    def update_step(cls, step_id, **data):
        step = cls.objects(id=step_id).first()
        if step:
            step.update(**data)
            step.reload()
        return step

    @classmethod
    def delete_step(cls, step_id):
        step = cls.objects(id=step_id).first()
        if step:
            step.delete()
            return True
        return False