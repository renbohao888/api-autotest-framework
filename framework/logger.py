# -*- coding: utf-8 -*-
"""日志层：控制台 + 按大小滚动的文件日志，统一格式，UTF-8。"""
import logging
import sys
from logging.handlers import RotatingFileHandler

from framework.conf import get_config, resolve

FORMAT = "%(asctime)s | %(levelname)-5s | %(name)-18s | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_ready = set()


def _attach_handlers(logger, level=None, log_file=None):
    cfg = get_config()
    level = (level or cfg.get("log.level", "INFO")).upper()
    logger.setLevel(level)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    logger.addHandler(console)

    if log_file is not False:
        target = resolve(log_file or cfg.get("log.file", "reports/atf.log"))
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(target, maxBytes=5 * 1024 * 1024,
                                           backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
        logger.addHandler(file_handler)
    logger.propagate = False


def get_logger(name="atf", log_file=None, level=None):
    """取（并按需初始化）一个 logger；同名 logger 只挂一次 handler。"""
    logger = logging.getLogger(name)
    key = (name, str(log_file))
    if key not in _ready:
        _attach_handlers(logger, level=level, log_file=log_file)
        _ready.add(key)
    return logger


def banner(text, logger=None):
    """输出一行醒目的分隔标题，便于在日志里定位每个阶段。"""
    (logger or get_logger()).info("=" * 12 + " %s " % text + "=" * 12)
