# -*- coding: utf-8 -*-
"""
数据营销决策分析竞赛平台 - 全自动调度器 (可恢复版)
时间段建房策略 + 满n开/40min强开 + 每20min自动翻期 + 自动结束

时间线 (北京时间):
  08:00-09:00  只建 牛刀小试
  09:00-12:00  主 锋芒毕露 次 牛刀小试
  12:00-14:00  只建 牛刀小试
  14:00-17:00  主 锋芒毕露 次 牛刀小试
  17:00-20:00  主 群雄争霸 次 锋芒毕露
  20:00-22:00  默认建 牛刀小试
  22:00后      不再新建, 等最后一局结束后收工

建房参数: 4季度, 每周期20分钟, 密码123, 其余默认
房间名: 尔尔定时比赛q群5342744003（满{n}开）不满{HH:MM}开
  满n开 = 人数到n即开始; 不满n人则建房时间+40min强制开始
翻期: 开始后每20min自动翻一期, 翻完4期自动结束

可恢复: 每次启动先检查是否有自己的房间在运行 -> 接管并继续监控/翻期/结束。
这样即使 GitHub Actions 每6小时重启 job, 也能无缝继续。

用法:
  python room_scheduler.py --login 自动房间-1 321
  python room_scheduler.py --login 自动房间-1 321 --once
  python room_scheduler.py --login 自动房间-1 321 --dry-run
"""
import argparse
import datetime as dt
import os
import re
import sys
import time
import urllib.parse

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://121.42.10.114:9997"
TZ_OFFSET = dt.timedelta(hours=8)  # 固定北京时间 (UTC+8)

# 场次定义: roomLevelId: 名称, 满n开阈值, 该场次最少人数
LEVELS = {
    1: {"name": "牛刀小试", "full_n": 6, "min_players": 3},
    2: {"name": "锋芒毕露", "full_n": 14, "min_players": 8},
    3: {"name": "群雄争霸", "full_n": 18, "min_players": 16},
    4: {"name": "精英荟萃", "full_n": 14, "min_players": 10},
    5: {"name": "济济一堂", "full_n": 20, "min_players": 20},
    6: {"name": "八仙过海", "full_n": 10, "min_players": 3},
}

ROOM_NAME_MARK = os.environ.get("ROOM_NAME_MARK", "尔尔定时比赛q群5342744003")
ROOM_NAME_TPL = ROOM_NAME_MARK + "（满{n}开）不满{time}开"
TOTAL_PERIOD = 4          # 4季度
PERIOD_LENGTH = 20        # 每周期20分钟 (翻期间隔)
ROOM_PASSWORD = "123"
FORCE_START_AFTER = 40    # 建房后40分钟强制开始
START_LIMIT_HOUR = 22     # 22点后不再新建房间
POLL_INTERVAL = 15        # 轮询秒数
MAX_JOB_RUNTIME = 5.9 * 60 * 60  # GitHub Actions job 限6小时, 留余量
FLIP_RETRY = 3            # 翻期失败重试次数

# 9001 决策软件端口
BASE_9001 = os.environ.get("BASE_9001", "http://121.42.10.114:9001")

# 参赛账号: (用户名, 密码)
# 唯一账号: 自动房间-1
ALL_ACCOUNTS = [
    ("自动房间-1", "321"),
]


class DecisionClient:
    """9001 决策软件客户端: 登录 + 提交全0决策"""

    def __init__(self, timeout=15):
        self.timeout = timeout

    def _login(self, uid, room_id):
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        r = s.post(f"{BASE_9001}/room/gotoMatch",
                   data={"str": f"login?userId={uid}*roomId={room_id}"}, timeout=self.timeout)
        d = r.json()["Data"]
        r2 = s.post(f"{BASE_9001}/login/login", data={
            "loginName": d["loginName"], "loginPass": d["loginPass"],
            "loginType": d["loginType"], "expId": d["expId"], "lagOrVersionId": "102",
        }, timeout=self.timeout)
        user = r2.json()["Data"]
        ck = {
            "role": str(d["loginType"]), "lagOrVersionId": "102", "loginName": d["loginName"],
            "compId": str(user.get("companyId")), "companyCode": str(user.get("companyCode")),
            "isLeader": str(user.get("leaderValue", 0)), "isFirstLogin": "0",
            "expId": str(user.get("expId")), "classID": str(user.get("classId")),
            "uid": str(user.get("userId")), "token": str(user.get("token") or user.get("userId")),
            "periodNum": str(user.get("periodNum", 1)),
            "totalPeriod": str(user.get("exp", {}).get("totalPeriod", 4)) if isinstance(user.get("exp"), dict) else "4",
            "userName": urllib.parse.quote(str(user.get("userName"))),
            "className": urllib.parse.quote(str(user.get("className"))),
            "roleTypeId": "1",
        }
        for k, v in ck.items():
            s.cookies.set(k, v, domain="121.42.10.114", path="/")
        return s, user, ck

    def get_uid(self, username):
        """根据用户名查 uid (从 roomIndex)"""
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        s.get("http://121.42.10.114:9997/login.jsp", timeout=self.timeout)
        s.post("http://121.42.10.114:9997/roomLogin/login",
               data={"loginName": username, "loginPass": self._pwd_for(username)}, timeout=self.timeout)
        idx = s.get("http://121.42.10.114:9997/room/roomIndex", timeout=self.timeout).text
        m = re.search(r"userId\s*=\s*['\"](\d+)['\"]", idx)
        return m.group(1) if m else None

    def _pwd_for(self, username):
        for u, p in ALL_ACCOUNTS:
            if u == username:
                return p
        return None

    def submit_decision(self, username, room_id, period_num):
        """提交全0决策 (type=1销售预测/2研发投入/3生产量). 返回是否全部成功"""
        uid = self.get_uid(username)
        if not uid:
            print(f"    [决策] {username} 无法获取uid")
            return False
        try:
            s, user, ck = self._login(uid, room_id)
        except Exception as e:
            print(f"    [决策] {username} 登录9001失败: {e}")
            return False
        ok = True
        for typ, sval in [(1, "0,0,0,0,0,0,0,0,0,"), (2, "0,0,0,0,0,0,"), (3, "0,0,0,0,0,0,0,0,0,")]:
            p = {
                "type": str(typ), "periodNum": str(period_num), "num": "1",
                "companyId": str(user.get("companyId")), "expId": str(user.get("expId")),
                "userId": str(user.get("userId")), "userName": ck["userName"],
                "className": ck["className"], "lagOrVersionId": "102", "str": sval,
            }
            try:
                rr = s.post(f"{BASE_9001}/student/decisionInfo/saveDecisionInfo?"
                            + urllib.parse.urlencode(p), timeout=self.timeout)
                if "2000" not in rr.text:
                    ok = False
            except Exception:
                ok = False
        return ok

    def submit_all(self, room_id, period_num):
        """所有账号提交全0决策, 返回成功数"""
        ok_count = 0
        for username, pwd in ALL_ACCOUNTS:
            try:
                if self.submit_decision(username, room_id, period_num):
                    ok_count += 1
                    print(f"    [决策] {username} 提交成功 ({ok_count})")
            except Exception as e:
                print(f"    [决策] {username} 异常: {e}")
        print(f"  [决策] 本轮 {ok_count}/{len(ALL_ACCOUNTS)} 个账号提交全0决策")
        return ok_count


class Scheduler:
    def __init__(self, username, password, timeout=15):
        self.username = username
        self.password = password
        self.user_id = None
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        self.timeout = timeout

    def _now(self):
        return dt.datetime.utcnow() + TZ_OFFSET

    def _post(self, path, **params):
        url = f"{BASE}{path}"
        if params:
            enc = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items())
            url += "?" + enc
        r = self.session.post(url, timeout=self.timeout)
        return r.text.strip()

    def _get(self, path, **params):
        url = f"{BASE}{path}"
        if params:
            enc = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items())
            url += "?" + enc
        r = self.session.get(url, timeout=self.timeout)
        if r.apparent_encoding:
            r.encoding = r.apparent_encoding
        else:
            r.encoding = "gbk"
        return r.text

    def login(self):
        r = self.session.post(f"{BASE}/roomLogin/login",
                              data={"loginName": self.username, "loginPass": self.password},
                              timeout=self.timeout)
        resp = r.text.strip()
        if resp == "1":
            idx = self._get("/room/roomIndex")
            m = re.search(r"userId\s*=\s*['\"](\d+)['\"]", idx)
            self.user_id = m.group(1) if m else None
            print(f"[OK] 登录成功: {self.username} (userId={self.user_id})")
            return True
        print(f"[FAIL] 登录失败: {resp}")
        return False

    def room_clean(self, room_level):
        try:
            self._post("/room/roomClean", userId=self.user_id, roomLevelId=room_level)
        except Exception as e:
            print(f"  [清理] 异常: {e}")

    def find_own_rooms(self, mark=None):
        """扫描所有场次, 返回 {room_level: room_id} 自己的房间(名字含标记)"""
        mark = mark or ROOM_NAME_MARK
        found = {}
        for lv in LEVELS:
            try:
                html = self._get("/room/gotoAddRoom", userId=self.user_id, roomLevelId=lv)
                for block in re.split(r'<div class="col-11 px-2 mb-3 room-list-item">', html):
                    if mark in block:
                        m = re.search(r"gotoJoinRoom\('\d+','\d+','(\d+)'\)", block)
                        if m:
                            found[lv] = m.group(1)
            except Exception:
                continue
        return found

    def room_status(self, room_id, room_level):
        """读取房间人数 返回 (当前人数, 上限) 或 (None, None)"""
        try:
            html = self._get("/room/gotoJoinRoom",
                             userId=self.user_id, roomLevelId=room_level, roomId=room_id)
            m = re.search(r"(\d+)/(\d+)", html)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        return None, None

    def is_room_finished(self, room_id, room_level):
        """判断房间是否已结束(列表按钮显示已结束)"""
        try:
            html = self._get("/room/gotoAddRoom", userId=self.user_id, roomLevelId=room_level)
            for block in re.split(r'<div class="col-11 px-2 mb-3 room-list-item">', html):
                if f"'{room_id}'" in block and ROOM_NAME_MARK in block:
                    return "已结束" in block
        except Exception:
            pass
        return False

    def start_exp(self, room_id, room_level):
        return self._post("/room/startRoomExp", type="1", roomId=room_id, userId=self.user_id)

    def next_period(self, room_id, room_level):
        return self._post("/room/startRoomExp", type="2", roomId=room_id, userId=self.user_id)

    def finish_exp(self, room_id, room_level):
        return self._post("/room/startRoomExp", type="3",
                          roomId=room_id, userId=self.user_id, roomLevelId=room_level)

    def plan_level(self):
        """根据当前北京时间返回 (主场次, 次场次) 或 None表示收工"""
        now = self._now()
        h = now.hour + now.minute / 60
        if 8 <= h < 9:
            return (1, 1)
        if 9 <= h < 12:
            return (2, 1)
        if 12 <= h < 14:
            return (1, 1)
        if 14 <= h < 17:
            return (2, 1)
        if 17 <= h < 20:
            return (3, 2)
        if 20 <= h < START_LIMIT_HOUR:
            return (1, 1)
        return None

    def create_room(self, room_level, created_at, room_name=None):
        level = LEVELS[room_level]
        n = level["full_n"]
        force_time = created_at + dt.timedelta(minutes=FORCE_START_AFTER)
        if room_name is None:
            name = ROOM_NAME_TPL.format(n=n, time=force_time.strftime("%H:%M"))
        else:
            name = room_name
        self.room_clean(room_level)
        params = {
            "userId": self.user_id, "roomLevelId": room_level,
            "roomName": name, "roomPassword": ROOM_PASSWORD,
            "totalPeriod": TOTAL_PERIOD, "isNeed": "0",
            "roomPeriodLength": PERIOD_LENGTH,
        }
        try:
            resp = self._post("/room/addRoom", **params)
        except Exception as e:
            print(f"  [建房] 异常: {e}", flush=True)
            return False, None
        print(f"  [建房] addRoom 返回: {resp}", flush=True)
        if resp in ("0", "2", "3"):
            print(f"  [建房] 失败: 返回码{resp}", flush=True)
            return False, None
        # addRoom 返回码是成功标志, 不是房号, 需从列表查实际房号
        time.sleep(2)
        room_id = self.find_own_rooms(mark=name).get(room_level)
        if room_id is None:
            # 可能名字含特殊字符匹配失败, 尝试用标记搜索
            mark = name[:6] if len(name) > 6 else name
            room_id = self.find_own_rooms(mark=mark).get(room_level)
        print(f"  [建房] 成功! 场次{room_level}({level['name']}) 满{n}开 房号{room_id} 名[{name}]", flush=True)
        return True, room_id

    def wait_and_start(self, room_id, room_level, n, created_at):
        """等满n人 或 到40min强制开. 返回是否成功开始"""
        force_time = created_at + dt.timedelta(minutes=FORCE_START_AFTER)
        print(f"  [等待] 房号{room_id} 满{n}人开, 未满则 {force_time.strftime('%H:%M')} 强制开", flush=True)
        while self._now() < force_time:
            players, maxp = self.room_status(room_id, room_level)
            if players is None:
                print(f"  [检测] 获取人数失败, {POLL_INTERVAL}s后重试", flush=True)
                time.sleep(POLL_INTERVAL)
                continue
            print(f"  [检测] {self._now().strftime('%H:%M:%S')} 人数 {players}/{maxp} (目标{n})", flush=True)
            if players >= n:
                print(f"  [触发] 已满{n}人, 立即开始!", flush=True)
                resp = self.start_exp(room_id, room_level)
                print(f"  [开始] 返回码: {resp}", flush=True)
                return resp == "1"
            time.sleep(POLL_INTERVAL)
        print(f"  [强制] 到点未满{n}人, 强制开始", flush=True)
        resp = self.start_exp(room_id, room_level)
        print(f"  [开始] 返回码: {resp}", flush=True)
        return resp == "1"

    def flip_loop(self, room_id, room_level, start_time=None, dc=None):
        """翻期循环: 每PERIOD_LENGTH分钟翻一期, 直到总季度数. 若已结束则提前返回"""
        total_flips = TOTAL_PERIOD - 1
        for i in range(total_flips):
            # 若房间已结束则停止
            if self.is_room_finished(room_id, room_level):
                print("  [翻期] 房间已结束, 停止翻期")
                return True
            print(f"  [翻期] 等待 {PERIOD_LENGTH} 分钟后翻第{i+2}期...")
            time.sleep(PERIOD_LENGTH * 60)
            # 翻期前先让所有账号提交全0决策
            if dc:
                period_now = i + 2
                print(f"  [决策] 翻第{period_now}期前提交全0决策...")
                dc.submit_all(room_id, period_now)
            resp = self.next_period(room_id, room_level)
            print(f"  [翻期] 第{i+2}期 返回码: {resp}")
            if resp != "1":
                for attempt in range(FLIP_RETRY):
                    print(f"    重试({attempt+1}/{FLIP_RETRY})...")
                    time.sleep(30)
                    resp = self.next_period(room_id, room_level)
                    print(f"    返回码: {resp}")
                    if resp == "1":
                        break
        # 全部季度完成, 提交最后一期决策后结束
        if dc:
            print("  [决策] 最后一期提交全0决策...")
            dc.submit_all(room_id, TOTAL_PERIOD)
        print("  [结束] 全部季度完成, 结束实验")
        resp = self.finish_exp(room_id, room_level)
        print(f"  [结束] 返回码: {resp}")
        if resp != "1":
            for attempt in range(FLIP_RETRY):
                print(f"    结束重试({attempt+1}/{FLIP_RETRY})...")
                time.sleep(30)
                resp = self.finish_exp(room_id, room_level)
                print(f"    返回码: {resp}")
                if resp == "1":
                    break
        return True

    def handle_room(self, room_id, room_level, created_at=None):
        """处理一个房间的完整生命周期(等待开始->翻期->结束).
        若已开始则直接进入翻期循环. 返回 True 表示处理完(结束/移交下一job)
        """
        level = LEVELS[room_level]
        n = level["full_n"]
        dc = DecisionClient(self.timeout)
        if created_at is None:
            created_at = self._now() - dt.timedelta(minutes=FORCE_START_AFTER)
        # 检查是否已开始 (若已过强制时间则直接翻期阶段)
        players, maxp = self.room_status(room_id, room_level)
        print(f"  [接管] 房号{room_id} 场次{room_level}({level['name']}) 当前 {players}/{maxp}", flush=True)
        if players is None:
            print("  [接管] 房间不存在或不可访问", flush=True)
            return True
        started = self.start_exp(room_id, room_level)
        if started == "1":
            print("  [接管] 房间已开始, 进入翻期循环", flush=True)
        elif "500" in str(started) or "error" in str(started).lower():
            # 500错误 = 房间可能已经开始了(重复调用start会500)
            print(f"  [接管] start返回服务器错误, 假设已开始, 直接进入翻期", flush=True)
        else:
            print(f"  [接管] 尝试开始返回{started}, 进入等待/强制开始流程", flush=True)
            ok = self.wait_and_start(room_id, room_level, n, created_at)
            if not ok:
                return False
        self.flip_loop(room_id, room_level, dc=dc)
        return True

    def run(self, dry_run=False):
        if not self.login():
            return False
        start_ts = time.time()
        print(f"=== 调度器启动 {self._now().strftime('%Y-%m-%d %H:%M:%S')} (北京) ===", flush=True)

        while True:
            # 检查是否接近job时限, 提前退出交给下个job
            elapsed_min = (time.time() - start_ts) / 60
            if time.time() - start_ts > MAX_JOB_RUNTIME:
                print("=== 接近job时限(6h), 退出, 下个job将接管 ===", flush=True)
                return True

            now = self._now()
            print(f"\n[{now.strftime('%H:%M:%S')}] 主循环 已运行{elapsed_min:.0f}min", flush=True)

            # 1. 先检查时间, 决定建房类型
            plan = self.plan_level()
            if plan is None:
                # 已过22点, 只处理未结束的房间
                print(f"[{now.strftime('%H:%M')}] 已过{START_LIMIT_HOUR}点, 检查未结束房间...", flush=True)
                own = self.find_own_rooms()
                if own:
                    for lv, rid in own.items():
                        if not self.is_room_finished(rid, lv):
                            print(f"[接管] 场次{lv} 房号{rid} 未结束, 继续处理", flush=True)
                            self.handle_room(rid, lv)
                            break
                    else:
                        print("所有房间已结束, 收工", flush=True)
                        return True
                    continue
                else:
                    print("无标记房间, 收工", flush=True)
                    return True

            primary, secondary = plan
            print(f"  计划: 主{LEVELS[primary]['name']} 次{LEVELS[secondary]['name']}", flush=True)

            # 2. 检查是否有自己是房主的未完成房间需要接管
            own = self.find_own_rooms()
            print(f"  找到 {len(own)} 个标记房间", flush=True)
            if own:
                for lv, rid in own.items():
                    if not self.is_room_finished(rid, lv):
                        print(f"[接管] 场次{lv} 房号{rid} 未结束, 接管处理", flush=True)
                        self.handle_room(rid, lv)
                        break
                else:
                    print("  现有房间均已结束, 可建新房", flush=True)
                continue

            # 3. 无进行中房间 -> 按时间计划建房
            room_level = primary if primary == secondary else self.pick_level(primary, secondary)
            print(f"  选定场次: {room_level}({LEVELS[room_level]['name']})", flush=True)

            if dry_run:
                name = ROOM_NAME_TPL.format(
                    n=LEVELS[room_level]["full_n"],
                    time=(now + dt.timedelta(minutes=FORCE_START_AFTER)).strftime("%H:%M"))
                print(f"  [DRY] 将建房间: {name} (4季度 20min)", flush=True)
                return True

            print(f"  [建房] 开始建场次{room_level}房间...", flush=True)
            created_at = self._now()
            ok, room_id = self.create_room(room_level, created_at)
            if not ok:
                print("  [建房] 失败, 2分钟后重试", flush=True)
                time.sleep(120)
                continue
            print(f"  [建房] 成功! 房号{room_id}", flush=True)
            self.handle_room(room_id, room_level, created_at)

    def pick_level(self, primary, secondary):
        """主/次/默认选择场次. 主满->试次, 次满->默认牛刀小试"""
        try:
            html = self._get("/room/gotoAddRoom", userId=self.user_id, roomLevelId=primary)
            if "爆满" not in html:
                return primary
        except Exception:
            pass
        print(f"  [场次] 主{LEVELS[primary]['name']} 爆满, 试次{LEVELS[secondary]['name']}")
        try:
            html = self._get("/room/gotoAddRoom", userId=self.user_id, roomLevelId=secondary)
            if "爆满" not in html:
                return secondary
        except Exception:
            pass
        print("  [场次] 次也爆满, 默认牛刀小试")
        return 1


def main():
    ap = argparse.ArgumentParser(description="竞赛平台全自动调度器")
    ap.add_argument("--login", nargs=2, metavar=("USER", "PASS"), required=True)
    ap.add_argument("--once", action="store_true", help="只处理一轮后退出")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不建房")
    ap.add_argument("--room-name", default=None, help="自定义房间名(默认带 满n开 提示)")
    args = ap.parse_args()

    sched = Scheduler(args.login[0], args.login[1])
    if args.once:
        if not sched.login():
            sys.exit(1)
        own = sched.find_own_rooms()
        plan = sched.plan_level()
        if not own and plan is None:
            print("当前不在建房时段且无进行中房间")
            return
        if own:
            for lv, rid in own.items():
                if not sched.is_room_finished(rid, lv):
                    sched.handle_room(rid, lv)
                    return
        if plan:
            primary, secondary = plan
            room_level = primary if primary == secondary else sched.pick_level(primary, secondary)
            if args.dry_run:
                now = sched._now()
                print("DRY 将建:", ROOM_NAME_TPL.format(
                    n=LEVELS[room_level]["full_n"],
                    time=(now + dt.timedelta(minutes=FORCE_START_AFTER)).strftime("%H:%M")))
                return
            created_at = sched._now()
            ok, room_id = sched.create_room(room_level, created_at, room_name=args.room_name)
            if ok:
                sched.handle_room(room_id, room_level, created_at)
    else:
        sched.run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()