# -*- coding: utf-8 -*-
"""被测系统（SUT）：简化版限量秒杀服务（含库存、幂等、限流、订单状态机）。"""
from service.app import create_app

__all__ = ["create_app"]
