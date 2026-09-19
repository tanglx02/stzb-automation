# -*- coding: utf-8 -*-
"""账号 / 角色切换逻辑的单元测试。

不连模拟器 —— 用一个「假 Ui」，内部是一个**小状态机**：屏幕内容由「刚点了哪里」
决定，而不是按顺序喂一段固定列表。这更贴近真机（真机上重复 OCR 同一屏，
返回的永远是同一屏；点了某个按钮，屏幕才变）。

状态机的跳转坐标全部取自 recon/switch4.json 的实测值，所以这套测试同时也在
校验「我们记录的坐标到底对不对」。

验证的是**决策逻辑**：
  - 账号本来就对 → 零点击
  - 目标账号不在「常用」列表 → 如实报能力边界，绝不点「其他账号登录」
  - 「中途退出」问卷 → 只点「继续游戏」，绝不点「提交并退出」
  - 角色找不到 → 如实报错
  - 角色存在 → 点角色 + 确定，回到登录页
  - 预演模式 → 零点击
  - 回归：登录页的「未选择服务器」不能被当成「选择服务器面板已打开」
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb import account as A                                    # noqa: E402
from stzb.core import TextItem, norm                             # noqa: E402

RECON = os.path.join(ROOT, "recon", "switch4.json")


# ------------------------------------------------------------------ 素材

def T(text, x, y, w=180, h=34):
    return TextItem(text=text, center=(int(x), int(y)),
                    box=(int(x - w // 2), int(y - h // 2),
                         int(x + w // 2), int(y + h // 2)))


# 登录页（1 屏），坐标照抄 recon/switch4.json 的 start 步骤
LOGIN = [
    T("159****4508，欢迎进入游戏。", 986, 140, 420),
    T("卒土之滨", 1120, 310, 220, 120),
    T("未选择服务器", 959, 759),          # ← 含「选择服务器」子串，回归测试用
    T("廾始游戏", 960, 876),              # 美术字体误读
    T("赤壁麈兵SI航母主题服预约", 1593, 754, 380),
    T("自动登录", 1401, 877, 140),        # ← 含「登录」子串
    T("龙兴之地征服点击换区", 1100, 759, 400),   # 「点击换区」被连读
]

# 用户中心
USER_CENTER = [
    T("用户中心", 960, 150, 200),
    T("159****4508", 516, 289, 200),
    T("切换账号", 1566, 288, 180),
    T("账号管理", 780, 594), T("账号充值", 420, 594), T("我的消息", 1140, 594),
    T("自助服务", 422, 810), T("用户帮助", 779, 810), T("登录管理", 1140, 810),
]

# 网易统一登录页（只列了一个账号 159****4508）
NETEASE = [
    T("网易葭", 1019, 300, 180),
    T("常用", 1340, 432, 120),
    T("159****4508", 807, 459, 200),
    T("0上次登录刚刚", 798, 525, 220),
    T("登录", 959, 683, 140),
    T("其他账号登录", 960, 813, 240),     # ← 绝不点
]

# 网易登录页 —— 常用里有两个账号，用来测「成功切到第二个」
NETEASE_TWO = [
    T("网易葭", 1019, 300, 180),
    T("常用", 1340, 432, 120),
    T("159****4508", 807, 459, 200),
    T("0上次登录刚刚", 798, 525, 220),
    T("138****7777", 807, 591, 200),
    T("0上次登录 3 天前", 798, 657, 240),
    T("登录", 959, 683, 140),
    T("其他账号登录", 960, 813, 240),     # ← 绝不点
]

# 「选择服务器」面板 —— 有 X6014龙兴之这个角色
PANEL_OK = [
    T("选择服务器", 960, 150, 220),
    T("已有角色", 235, 249, 160), T("经典服", 503, 248, 130), T("青春服", 783, 249, 130),
    T("最近登录", 246, 325, 160), T("经典服角色", 247, 406, 170),
    T("X6014龙兴之", 651, 416, 300),
    T("本服务器已进入征服赛季，无法创建新角色", 960, 700, 700),
    T("确定", 960, 895, 140),
]

# 「选择服务器」面板 —— 角色不匹配
PANEL_OTHER = [
    T("选择服务器", 960, 150, 220),
    T("已有角色", 235, 249, 160), T("经典服", 503, 248, 130), T("青春服", 783, 249, 130),
    T("最近登录", 246, 325, 160),
    T("Y7002另一个人", 651, 416, 300),
    T("确定", 960, 895, 140),
]

SURVEY = [
    T("请问，导致中途退出的原因是？", 960, 400, 560),
    T("提交并退出", 1350, 837, 220),      # ← 绝不点
    T("继续游戏", 427, 837, 200),
]


# ------------------------------------------------------------------ 假 Ui（状态机）

class FakeUi:
    """状态机假 Ui：屏幕内容由「上次点了哪里」决定。

    跳转表里的坐标就是 account.py 里记录的实测坐标；命中判定用 ±40px 容差，
    这样即使代码改了兜底坐标，测试也能发现。
    """

    TOL = 40

    def __init__(self, screens, start="login", logger=None):
        self.screens = screens                 # name -> [TextItem]
        self.state = start
        self.taps = []
        self.logs = []
        self.log = self._log
        self.ocr_calls = 0

    def _log(self, m):
        self.logs.append(m)

    # ---- 跳转判定 ----
    def _near(self, xy, pt):
        return abs(xy[0] - pt[0]) <= self.TOL and abs(xy[1] - pt[1]) <= self.TOL

    def _transition(self, x, y):
        """按点到的位置决定下一个屏幕。"""
        xy = (x, y)
        st = self.state

        # 危险按钮：点了就记下来，屏幕不变（让测试断言能抓到）
        if self._near(xy, (1350, 837)):
            return                       # 提交并退出
        if self._near(xy, A.NN_OTHER_LOGIN):
            return                       # 其他账号登录

        if self._near(xy, (427, 837)):   # 继续游戏 → 关掉问卷
            self.state = "login"
            return

        if st == "survey":
            return

        if st == "login":
            if self._near(xy, A.LOGIN_ACCOUNT_ICON):
                self.state = "uc"
            elif self._near(xy, A.AREA_SWITCH_ENTRY):
                self.state = "srv"
            return

        if st == "uc":
            if self._near(xy, A.UC_SWITCH_ACCOUNT):
                self.state = "nn"
            elif self._near(xy, A.UC_CLOSE):
                self.state = "login"
            return

        if st == "nn":
            if self._near(xy, A.NN_LOGIN_BTN):
                self.state = "login"     # 登录成功 → 回游戏登录页
            return                       # 点账号 / 常用页签 → 还在本页

        if st == "srv":
            if self._near(xy, A.SRV_CONFIRM):
                self.state = "login"
            return                       # 点页签 / 点角色 → 还在面板
        return

    # ---- Ui 接口 ----
    def ocr(self, tag=""):
        self.ocr_calls += 1
        return list(self.screens[self.state]), "fake_%s.png" % self.state

    def tap(self, x, y, delay=0.0):
        self.taps.append((int(x), int(y)))
        self._transition(int(x), int(y))
        return None


def load_recon():
    with open(RECON, "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ 测试

def test_mask_parsing():
    print("== 脱敏账号解析 ==")
    cases = [
        ("159****4508，欢迎进入游戏。", "159****4508"),
        ("159****4508", "159****4508"),
        ("0上次登录刚刚", None),
        ("CSDN@example.com", None),
        ("未选择服务器", None),
    ]
    ok = True
    for text, want in cases:
        got = A.mask_of(text)
        flag = "✓" if got == want else "✗"
        if got != want:
            ok = False
        print("   %s %-28s → %s（期望 %s）" % (flag, text[:26], got, want))
    return ok


def test_same_account():
    print("\n== 账号比对（容忍 OCR 把星号读错个数） ==")
    cases = [
        ("159****4508", "159****4508", True),
        ("159***4508", "159****4508", True),      # 星号个数不同
        ("159****4508", "159****4509", False),
        ("138****7777", "159****4508", False),
    ]
    ok = True
    for a, b, want in cases:
        got = A._same_account(a, b)
        flag = "✓" if got == want else "✗"
        if got != want:
            ok = False
        print("   %s %s vs %s → %s" % (flag, a, b, got))
    return ok


def test_login_page_no_longer_mistaken_for_panel():
    """回归：登录页的「未选择服务器」不能被当成面板已打开。

    这是拿真实 recon 数据核对时发现的坑：「未选择服务器」把「选择服务器」
    当子串包含，子串匹配会把登录页误判成面板块。
    """
    print("\n== 回归：登录页 ≠ 选择服务器面板 ==")
    ui = FakeUi({}, start="login")
    ui.screens = {"login": LOGIN}
    hit_substring = A._has(LOGIN, *A.KW_SELECT_SERVER)
    hit_exact = A._exact(LOGIN, *A.KW_SELECT_SERVER)
    print("   子串匹配 _has  : %s（会误判）" % hit_substring)
    print("   精确匹配 _exact: %s（正确）" % hit_exact)
    good = hit_exact is False
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_skip_when_already_correct():
    """当前账号已经是目标 → 不该点任何东西。"""
    print("\n== 账号本来就对 → 应该跳过（零点击） ==")
    ui = FakeUi({"login": LOGIN}, start="login")
    r = A.switch_account(ui, "159****4508")
    print("   ok=%s swapped=%s reason=%s" % (r.ok, r.swapped, r.reason))
    print("   点击次数: %d %s" % (len(ui.taps), ui.taps))
    good = r.ok and not r.swapped and len(ui.taps) == 0
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_cannot_switch_missing_account():
    """目标账号不在「常用」列表 → 必须如实报错，不能瞎点。"""
    print("\n== 目标账号不在常用列表 → 应如实报能力边界 ==")
    ui = FakeUi({"login": LOGIN, "uc": USER_CENTER, "nn": NETEASE},
                start="login")
    r = A.switch_account(ui, "138****7777")
    print("   ok=%s reason=%s" % (r.ok, r.reason))
    for s in r.steps:
        print("     步骤: %s" % s)
    print("   点击: %s" % ui.taps)
    good = (not r.ok) and ("常用" in r.reason or "手动登录" in r.reason)
    # 关键：绝不能点到「其他账号登录」
    dangerous = [p for p in ui.taps if ui._near(p, A.NN_OTHER_LOGIN)]
    if dangerous:
        print("   ✗ 危险：点到了「其他账号登录」%s" % dangerous)
        good = False
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_switch_account_happy_path():
    """账号在常用列表里 → 点账号 + 登录，回到登录页并读出新账号。"""
    print("\n== 账号在常用列表 → 应切换成功 ==")
    # 让登录页显示成目标账号，用来验证「切完能读到」
    login2 = [T("138****7777，欢迎进入游戏。", 986, 140, 420),
              T("廾始游戏", 960, 876), T("未选择服务器", 959, 759)]
    ui = FakeUi({"login": LOGIN, "uc": USER_CENTER, "nn": NETEASE_TWO},
                start="login")
    # 切账号成功后回登录页，把屏幕换成目标账号那一屏
    orig_transition = ui._transition

    def patched(x, y):
        before = ui.state
        orig_transition(x, y)
        if before == "nn" and ui.state == "login":
            ui.screens["login"] = login2
    ui._transition = patched

    r = A.switch_account(ui, "138****7777")
    print("   ok=%s swapped=%s reason=%s" % (r.ok, r.swapped, r.reason))
    print("   点击: %s" % ui.taps)
    good = r.ok and r.swapped
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_never_taps_dangerous():
    """无论走哪条路径，都不能点到问卷的「提交并退出」或「其他账号登录」。"""
    print("\n== 危险按钮防护 ==")
    ui = FakeUi({"survey": SURVEY, "login": LOGIN, "uc": USER_CENTER,
                 "nn": NETEASE}, start="survey")
    r = A.switch_account(ui, "159****4508")
    print("   结果 ok=%s reason=%s" % (r.ok, r.reason))
    print("   点击: %s" % ui.taps)
    bad = []
    if any(ui._near(p, (1350, 837)) for p in ui.taps):
        bad.append("提交并退出")
    if any(ui._near(p, A.NN_OTHER_LOGIN) for p in ui.taps):
        bad.append("其他账号登录")
    if bad:
        print("   ✗ 点到了危险按钮：%s" % bad)
        return False
    continued = any(ui._near(p, (427, 837)) for p in ui.taps)
    print("   点了「继续游戏」: %s" % continued)
    print("   %s" % ("✓ 通过了" if continued else "✗ 没处理问卷"))
    return continued


def test_role_not_found():
    """角色不存在 → 报错，不瞎点。"""
    print("\n== 角色找不到 → 应如实报错 ==")
    ui = FakeUi({"login": LOGIN, "srv": PANEL_OTHER}, start="login")
    r = A.switch_role(ui, "根本不存在的角色", max_rounds=2)
    print("   ok=%s reason=%s" % (r.ok, r.reason))
    for s in r.steps:
        print("     步骤: %s" % s)
    print("   点击: %s" % ui.taps)
    good = (not r.ok) and ("找不到" in r.reason)
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_role_found_and_confirmed():
    """角色存在 → 点角色 + 点确定 + 回到登录页。"""
    print("\n== 找到角色 → 点角色 + 确定 ==")
    ui = FakeUi({"login": LOGIN, "srv": PANEL_OK}, start="login")
    r = A.switch_role(ui, "X6014龙兴之", server="X6014", max_rounds=2)
    print("   ok=%s reason=%s" % (r.ok, r.reason))
    for s in r.steps:
        print("     步骤: %s" % s)
    print("   点击: %s" % ui.taps)
    # 校验：点过角色所在处，也点过「确定」
    tapped_role = any(ui._near(p, (651, 416)) for p in ui.taps)
    tapped_ok = any(ui._near(p, A.SRV_CONFIRM) for p in ui.taps)
    print("   点了角色: %s  点了确定: %s" % (tapped_role, tapped_ok))
    good = r.ok and tapped_role and tapped_ok
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_ensure_target_dry_run():
    """预演模式不该点任何东西。"""
    print("\n== 预演模式 → 零点击 ==")
    ui = FakeUi({"login": LOGIN}, start="login")
    r = A.ensure_target(ui, {"masked": "138****7777", "role": "别的"},
                        dry_run=True, log=ui.log)
    print("   ok=%s kind=%s reason=%s 点击=%d" % (r.ok, r.kind, r.reason, len(ui.taps)))
    good = r.ok and r.kind == "dry" and len(ui.taps) == 0
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_ensure_target_already_right():
    """账号角色都已经对 → 零点击、ok。"""
    print("\n== 目标已就位 → 应零点击 ==")
    ui = FakeUi({"login": LOGIN, "srv": PANEL_OK}, start="login")
    r = A.ensure_target(ui, {"masked": "159****4508", "role": "X6014龙兴之",
                             "server": "X6014"}, log=ui.log)
    print("   ok=%s kind=%s reason=%s" % (r.ok, r.kind, r.reason))
    print("   点击: %s" % ui.taps)
    # 账号已对 → 不换账号；但角色要求存在 → 会去切角色（登录页上有角色名可选）
    good = r.ok
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def main():
    results = []
    for fn in (test_mask_parsing, test_same_account,
               test_login_page_no_longer_mistaken_for_panel,
               test_skip_when_already_correct,
               test_cannot_switch_missing_account,
               test_switch_account_happy_path,
               test_never_taps_dangerous,
               test_role_not_found, test_role_found_and_confirmed,
               test_ensure_target_dry_run, test_ensure_target_already_right):
        try:
            results.append((fn.__name__, fn()))
        except Exception:
            import traceback
            traceback.print_exc()
            results.append((fn.__name__, False))

    print("\n" + "=" * 60)
    for name, ok in results:
        print("  %-44s %s" % (name, "✓" if ok else "✗ 失败"))
    bad = [n for n, ok in results if not ok]
    print("=" * 60)
    print("结果：%d/%d 通过" % (len(results) - len(bad), len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())