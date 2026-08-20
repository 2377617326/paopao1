# -*- coding: utf-8 -*-
"""
数据营销决策分析竞赛平台 房间自动化脚本
功能: 登录 -> 创建房间 -> 检测人数 -> 开始实验(翻期) -> 结束实验
支持: Python 3 (requests), 可部署到任意服务器/GitHub Actions

用法示例:
  python room_bot.py --login beat-14 114 --create
  python room_bot.py --login beat-14 114 --create --auto-start --min-players 3 --periods 2
  python room_bot.py --login beat-14 114 --status
  python room_bot.py --login beat-14 114 --create --monitor --min-players 3 --max-wait 3600
"""
import argparse
import re
import sys
import time

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://121.42.10.114:9997"
ROOM_LEVEL = "1"          # 场次: 1=牛刀小试(3-8人)
TOTAL_PERIOD = "2"        # 总季度数
ROOM_PERIOD_LENGTH = "20" # 每季度分钟数
ROOM_PASSWORD = "123"     # 无密码时默认123


class RoomBot:
    def __init__(self, username, password, timeout=15):
        self.username = username
        self.password = password
        self.user_id = None
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        self.timeout = timeout

    def _post(self, path, **params):
        """POST 请求, 参数拼到 URL(与前端一致)"""
        url = f"{BASE}{path}"
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        r = self.session.post(url, timeout=self.timeout)
        return r.text.strip()

    def _get(self, path, **params):
        url = f"{BASE}{path}"
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        r = self.session.get(url, timeout=self.timeout)
        # 页面编码不固定(GBK/UTF-8), 自动检测
        if r.apparent_encoding:
            r.encoding = r.apparent_encoding
        else:
            r.encoding = "gbk"
        return r.text

    def login(self):
        """登录, 成功返回True并取得userId"""
        r = self.session.post(
            f"{BASE}/roomLogin/login",
            data={"loginName": self.username, "loginPass": self.password},
            timeout=self.timeout,
        )
        resp = r.text.strip()
        if resp == "1":
            # 从roomIndex页面提取userId
            idx = self._get("/room/roomIndex")
            m = re.search(r"userId\s*=\s*['\"](\d+)['\"]", idx)
            self.user_id = m.group(1) if m else None
            print(f"[OK] 登录成功: {self.username} (userId={self.user_id})")
            return True
        if resp == "0":
            print(f"[FAIL] 用户名或密码错误: {self.username}")
        elif resp == "2":
            print(f"[FAIL] 账号被禁用: {self.username}")
        else:
            print(f"[FAIL] 登录异常, 返回: {resp}")
        return False

    def get_credit(self):
        """获取积分余额, 返回整数"""
        html = self._get("/roomLogin/rUserInfo")
        # 页面形如 "<b>-59</b><span>积分</span>", 数字与"积分"间可能有标签
        m = re.search(r"<b[^>]*>\s*(-?\d+)\s*</b>\s*<span[^>]*>\s*积分", html)
        return int(m.group(1)) if m else None

    def sign_in(self):
        """每日签到 +1积分"""
        resp = self._post("/rFraction/DK")
        print(f"[签到] 返回: {resp}")

    def room_clean(self, room_level=ROOM_LEVEL):
        """清理过期房间"""
        resp = self._post("/room/roomClean", userId=self.user_id, roomLevelId=room_level)
        print(f"[清理] 返回: {resp}")
        return resp

    def list_rooms(self, room_level=ROOM_LEVEL):
        """列出场次下所有房间, 返回 [(roomId, name, status)]"""
        html = self._get("/room/gotoAddRoom", userId=self.user_id, roomLevelId=room_level)
        rooms = []
        # 提取每个房间卡片: id, 名称, 状态(进行中/未开始)
        for m in re.finditer(
            r"gotoJoinRoom\('(\d+)','\d+','(\d+)'\).*?([\u4e00-\u9fa5A-Za-z0-9]+)",
            html, re.S,
        ):
            rooms.append((m.group(2), m.group(3), m.group(1)))
        return rooms

    def create_room(self, room_level=ROOM_LEVEL, room_name=None, password=ROOM_PASSWORD,
                    total_period=TOTAL_PERIOD, room_period_length=ROOM_PERIOD_LENGTH,
                    is_need="0"):
        """创建房间.
        返回: (成功?, 返回码, 提示)
        返回码含义: 0=房间已爆满, 2=有未结束实验, 3=非法数据, 其他=成功
        """
        name = room_name or f"bot{self.username[-2:]}{int(time.time())%100}"
        params = {
            "userId": self.user_id, "roomLevelId": room_level,
            "roomName": name, "roomPassword": password,
            "totalPeriod": total_period, "isNeed": is_need,
            "roomPeriodLength": room_period_length,
        }
        resp = self._post("/room/addRoom", **params)
        msg_map = {"0": "房间已爆满, 无法创建", "2": "有未结束的房间实验", "3": "含有非法数据"}
        if resp in msg_map:
            print(f"[创建失败] 返回码={resp}: {msg_map[resp]}")
            return False, resp, msg_map[resp]
        print(f"[创建成功] 房间名={name}, 返回码={resp}")
        return True, resp, name

    def find_room(self, room_id=None, room_level=ROOM_LEVEL):
        """从房间列表找自己的房间(名字含bot或指定的id), 返回roomId或None"""
        html = self._get("/room/gotoAddRoom", userId=self.user_id, roomLevelId=room_level)
        if room_id:
            return room_id if f"'{room_id}'" in html else None
        # 找名称含bot前缀的房间
        m = re.search(r"gotoJoinRoom\('(\d+)','\d+','(\d+)'.*?bot", html, re.S)
        if m:
            return m.group(2)
        return None

    def join_room(self, room_id, password=ROOM_PASSWORD, room_level=ROOM_LEVEL):
        """加入房间, 返回 (是否成功, 提示)"""
        resp = self._post("/room/joinRoomEvery",
                          roomId=room_id, userId=self.user_id, joinRoomPassword=password)
        msg_map = {
            "0": "房间已结束/不存在", "1": "加入成功", "2": "房间人数已满",
            "3": "已加入过该房间", "5": "积分不足",
        }
        msg = msg_map.get(resp, f"未知返回码{resp}")
        print(f"[加入] roomId={room_id}: {msg}")
        return resp == "1" or resp == "3", msg

    def room_status(self, room_id, room_level=ROOM_LEVEL):
        """获取房间状态: 人数(当前/上限), 状态, 是否房主.
        返回 dict
        """
        html = self._get("/room/gotoJoinRoom",
                         userId=self.user_id, roomLevelId=room_level, roomId=room_id)
        result = {"room_id": room_id, "players": None, "max": None, "status": None, "html": html}
        m = re.search(r"(\d+)/(\d+)", html)
        if m:
            result["players"], result["max"] = int(m.group(1)), int(m.group(2))
        return result

    def start_exp(self, room_id, room_level=ROOM_LEVEL):
        """开始实验. 返回码: 1=成功, 2=人数不足(需>=3人), 9=房间已开始"""
        resp = self._post("/room/startRoomExp", type="1", roomId=room_id, userId=self.user_id)
        msg_map = {"1": "实验已开始", "2": "人数不足, 至少3人", "9": "房间已开始"}
        print(f"[开始实验] roomId={room_id}: {msg_map.get(resp, '返回码'+resp)}")
        return resp == "1", resp

    def next_period(self, room_id, room_level=ROOM_LEVEL):
        """翻期 = 进入下一季度. 返回码: 1=成功, 0=失败"""
        resp = self._post("/room/startRoomExp", type="2", roomId=room_id, userId=self.user_id)
        print(f"[翻期] roomId={room_id}: {'成功' if resp == '1' else '失败(返回'+resp+')'}")
        return resp == "1", resp

    def finish_exp(self, room_id, room_level=ROOM_LEVEL):
        """结束实验. 返回码: 1=成功, 0=失败"""
        resp = self._post("/room/startRoomExp", type="3",
                          roomId=room_id, userId=self.user_id, roomLevelId=room_level)
        print(f"[结束实验] roomId={room_id}: {'成功' if resp == '1' else '失败(返回'+resp+')'}")
        return resp == "1", resp


def main():
    ap = argparse.ArgumentParser(description="竞赛平台房间自动化")
    ap.add_argument("--login", nargs=2, metavar=("USER", "PASS"), required=True,
                    help="账号密码")
    ap.add_argument("--create", action="store_true", help="创建房间")
    ap.add_argument("--room-level", default=ROOM_LEVEL)
    ap.add_argument("--room-name", default=None)
    ap.add_argument("--periods", default=TOTAL_PERIOD, help="总季度数")
    ap.add_argument("--period-length", default=ROOM_PERIOD_LENGTH, help="每季度分钟数")
    ap.add_argument("--monitor", action="store_true", help="监听人数并自动开始/翻期/结束")
    ap.add_argument("--min-players", type=int, default=3, help="开始实验所需最少人数")
    ap.add_argument("--max-wait", type=int, default=3600, help="monitor最大等待秒数")
    ap.add_argument("--poll-interval", type=int, default=10, help="monitor轮询间隔秒数")
    ap.add_argument("--status", action="store_true", help="查看积分/签到")
    ap.add_argument("--find", action="store_true", help="查找自己的房间")
    ap.add_argument("--start", action="store_true", help="开始实验")
    ap.add_argument("--period", action="store_true", help="翻期(进入下季度)")
    ap.add_argument("--finish", action="store_true", help="结束实验")
    ap.add_argument("--room-id", default=None, help="指定房间ID")
    args = ap.parse_args()

    bot = RoomBot(args.login[0], args.login[1])
    if not bot.login():
        sys.exit(1)

    if args.status:
        credit = bot.get_credit()
        print(f"[状态] 当前积分: {credit}")
        bot.sign_in()
        return

    if args.create:
        ok, code, msg = bot.create_room(
            room_level=args.room_level, room_name=args.room_name,
            total_period=args.periods, room_period_length=args.period_length,
        )
        if not ok and code == "0":
            print("[提示] 场次已爆满。可尝试: 1)换场次 2)roomClean清理过期房间 3)等待")
            return
        if not ok:
            return

    if args.find or args.monitor:
        room_id = args.room_id or bot.find_room(room_id=args.room_id)
        if not room_id:
            print("[FAIL] 未找到自己的房间")
            sys.exit(1)
        print(f"[OK] 房间ID: {room_id}")

    if args.monitor:
        room_id = args.room_id or bot.find_room(room_id=args.room_id)
        deadline = time.time() + args.max_wait
        started = False
        while time.time() < deadline:
            st = bot.room_status(room_id, args.room_level)
            players = st["players"]
            print(f"  [检测] 人数 {players}/{st['max']}")
            if players is None:
                time.sleep(args.poll_interval)
                continue
            if not started and players >= args.min_players:
                ok, code = bot.start_exp(room_id, args.room_level)
                if ok:
                    started = True
                    print("  [流程] 已开始实验, 等待玩家决策后翻期...")
            time.sleep(args.poll_interval)
        if started:
            ok, _ = bot.next_period(room_id, args.room_level)
            ok, _ = bot.finish_exp(room_id, args.room_level)
        return

    if args.start:
        room_id = args.room_id or bot.find_room(room_id=args.room_id)
        bot.start_exp(room_id, args.room_level)
    if args.period:
        room_id = args.room_id or bot.find_room(room_id=args.room_id)
        bot.next_period(room_id, args.room_level)
    if args.finish:
        room_id = args.room_id or bot.find_room(room_id=args.room_id)
        bot.finish_exp(room_id, args.room_level)


if __name__ == "__main__":
    main()