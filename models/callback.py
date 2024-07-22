#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/22
@Python：python 3.9
"""
import datetime
from mongoengine import *
import datetime
from . import db
from bson import ObjectId


class Callback(db.Document):
    union_uid = StringField()
    create_time = DateTimeField(default=datetime.datetime.now)

    meta = {
        'collection': 'callbacks',
        'ordering': ['-create_time']  # 默认按创建时间倒序排序
    }

    @classmethod
    def create_callback(cls, union_uid):
        callback = cls(union_uid=union_uid)
        callback.save()
        return callback

    @classmethod
    def get_callbacks_by_union_uid(cls, union_uid, since_time):
        if not isinstance(union_uid, str):
            raise ValidationError('union_uid must be a string')
        if not isinstance(since_time, datetime.datetime):
            raise ValidationError('since_time must be a datetime instance')
        return cls.objects(union_uid=union_uid, create_time__gt=since_time).order_by('-create_time')

    @classmethod
    def update_callback(cls, callback_id, **data):
        callback = cls.objects(id=callback_id).first()
        if callback:
            callback.update(**data)
            callback.reload()
        return callback

    @classmethod
    def delete_callback(cls, callback_id):
        callback = cls.objects(id=callback_id).first()
        if callback:
            callback.delete()
            return True
        return False