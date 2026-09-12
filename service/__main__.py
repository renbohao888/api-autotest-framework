# -*- coding: utf-8 -*-
"""独立启动被测服务（SUT）。

    python -m service --port 8899 --stock 10

启动后可配合 Postman / JMeter 手工验证，也可以把框架指向它：
    config/env/dev.yaml 里改 http.base_url，并把 sut.auto_start 设为 false。
"""
import argparse
import os
import tempfile

from service.app import create_app


def main():
    parser = argparse.ArgumentParser(description="秒杀被测服务（SUT）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--db", default=os.path.join(tempfile.gettempdir(), "atf_seckill.db"),
                        help="SQLite 数据库文件（默认放临时目录，重启即重置）")
    parser.add_argument("--stock", type=int, default=10, help="活动初始库存")
    parser.add_argument("--limit", type=int, default=3, help="同一用户每秒请求上限")
    args = parser.parse_args()

    app = create_app(args.db, args.limit)
    app.extensions["store"].reset(stock=args.stock)
    print("被测服务已启动：http://%s:%d ｜ 数据库 %s ｜ 库存 %d ｜ 限流 %d 次/秒"
          % (args.host, args.port, args.db, args.stock, args.limit))
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
