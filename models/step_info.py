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

class StepInfo(db.Document):
    step_id = StringField()
    create_time = DateTimeField(default=datetime.datetime.now)
    step_message = StringField()

    meta = {
        'collection': 'step_info',
        'ordering': ['create_time']  # 默认按时间正序排序
    }

    @classmethod
    def create_step_info(cls, step_id, step_message):
        step_info = cls(step_id=step_id, step_message=step_message)
        step_info.save()
        return step_info

    @classmethod
    def get_step_infos_by_step_id(cls, step_id):
        return cls.objects(step_id=step_id).order_by('create_time')

    @classmethod
    def update_step_info(cls, step_info_id, **data):
        step_info = cls.objects(id=step_info_id).first()
        if step_info:
            step_info.update(**data)
            step_info.reload()
        return step_info

    @classmethod
    def delete_step_info(cls, step_info_id):
        step_info = cls.objects(id=step_info_id).first()
        if step_info:
            step_info.delete()
            return True
        return False