# -*- coding: utf-8 -*-
"""离线自检：把实测遇到的 OCR 乱码/界面文字喂进匹配与选按钮逻辑，断言行为正确。

重点是安全规则：
  * 「20/征收」这类带数字的付费按钮绝不能被选中；
  * 特性面板只在「获取1张」且旁边有「免费」时才点；
  * 招募卡包详情页不能被误判成主城。

跑法：python tools/selftest.py
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb.core import TextItem, find_text, match_score, merge_items, norm  # noqa: E402
from stzb.ui import Ui                                                   # noqa: E402
from stzb.tasks import (_FallbackBtn, _band_jade_frac, _band_price,       # noqa: E402
                        _block_has_ink, _block_in_row, _block_labels,
                        _find_free_zz, _find_hufu_buy, _find_sweep_entry,
                        _find_sweep_icon, _free_claim_is_trustworthy, _got_reward,
                        _has_done_stamp, _has_discount_ribbon, _has_free_badge,
                        _hufu_exchange_ok, _is_free_zz_txt, _is_sweep_dialog,
                        _parse_clock, _parse_free_countdown,
                        _pick_free_button, _price_in_btn_row, _price_left_of,
                        _price_under_btn,
                        _shop_blocks, _sweep_claim_button, _sweep_countdown)

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, extra))


def it(text, x=0, y=0, w=100, h=30):
    return TextItem(text, (x, y, x + w, y + h), (x + w // 2, y + h // 2))


def itc(text, cx, cy, w=100, h=30):
    """按「中心点」造一条 OCR 结果（多数实测坐标都是中心点）。"""
    return TextItem(text, (cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2), (cx, cy))


def _blank(h=1080, w=1920):
    import numpy as np
    return np.zeros((h, w, 3), dtype=np.uint8)


def _btn(img, x, y, w=170, h=48, bgr=(245, 245, 245)):
    """在图上画一个按钮白块。"""
    img[y:y + h, x:x + w] = bgr
    return (x, y, x + w, y + h)


def _ink(img, x, y, w=170, h=48):
    """在白块上画点「黑字」，让 _block_has_ink 认得出里面有字。"""
    img[y + 14:y + h - 14, x + 40:x + w - 40] = (20, 20, 20)


def _jade(img, x, y, w=120, h=32):
    """画一个绿色的价格图标（玉符）。"""
    img[y:y + h, x:x + w] = (60, 200, 80)


# --------------------------------------------------------------------------- 1. 文本匹配

def test_match():
    print("\n[1] 文本匹配（游戏美术字体 OCR 误认）")
    cases = [
        ("犄性", "特性", True),
        ("持性", "特性", True),
        ("橈收", "税收", True),
        ("橈叫攵", "税收", True),
        ("市井", "市井", True),
        ("巿井", "市井", True),
        ("秀宝物商队", "宝物商队", True),
        ("获取1张", "获取1张", True),
        ("立鼠获取", "立即获取", True),
        ("20/征收", "征收", True),          # 含子串，匹配到是正常的
        ("立即征收2/3", "征收", True),
        ("17:17:24后可免费获取1张", "获取1张", True),
        ("已征收", "已征收", True),
        ("征兵", "征收", False),
        ("武将", "内政", False),
        ("铜钱", "玉符", False),
    ]
    for text, kw, expect in cases:
        got = match_score(text, kw) >= 0.62
        check("%-22s ~ %-8s -> %s" % (text, kw, got), got == expect,
              "期望 %s 实得 %s" % (expect, got))


# --------------------------------------------------------------------------- 2. 税收按钮

class FakeUi(Ui):
    def __init__(self):
        pass


def test_shuishou():
    print("\n[2] 税收面板：只点免费「征收」，绝不点「20/征收」")
    ui = FakeUi()

    # ① 三个格子全部已征收 / 征收中（没有任何付费按钮）-> 不该点任何东西
    items = [
        it("已征收", 443, 785), it("已征收", 955, 785),
        it("征收中01:50:09", 1466, 702), it("5000", 1466, 702),
        it("征收", 427, 784), it("征收", 948, 784), it("征收", 1467, 784),
    ]
    r = _find_free_zz(ui, items)
    check("全是已征收/征收中时 -> 返回 None (%r)" % (r,), r is None)

    # ①b 已征收/等待中，但旁边还挂着付费按钮 -> 返回 False
    items1b = items + [it("20/征丬攵", 1511, 877)]
    r1b = _find_free_zz(ui, items1b)
    check("已领完 + 挂着付费按钮 -> 返回 False", r1b is False, repr(r1b))

    # ② 有一个格子待征，且按钮文字是「征收」 -> 应该返回它
    items2 = [
        it("已征收", 443, 785), it("已征收", 955, 785),
        it("征收", 1467, 880),                     # 免费按钮
        it("征收", 427, 784), it("征收", 948, 784),
    ]
    r2 = _find_free_zz(ui, items2)
    check("有免费「征收」时选中它", r2 is not None and r2 is not False
          and "征收" in r2.text and not any(c.isdigit() for c in r2.text), repr(r2))
    if r2 is not None and r2 is not False:
        check("选中的按钮在按钮行(y>=835)", r2.center[1] >= 835, str(r2.center))

    # ③ 只有付费按钮 -> 必须返回 False（表示「只剩付费」）
    items3 = [
        it("已征收", 443, 785), it("已征收", 955, 785),
        it("征收", 427, 784), it("征收", 948, 784), it("征收", 1467, 784),
        it("20/征收", 1511, 877),
    ]
    r3 = _find_free_zz(ui, items3)
    check("只剩付费「20/征收」-> 返回 False", r3 is False, repr(r3))

    # ④ 某一格 OCR 什么都没读到 -> 必须是「什么都不点」。盲点固定坐标会出人命：
    #    2026-09-18 实测那一格当时是「20/强征」，盲点直接弹「虎符不足」。
    items4 = [
        it("已征收", 443, 785), it("已征收", 955, 785),
        it("征收", 427, 784), it("征收", 948, 784), it("征收", 1467, 784),
    ]
    r4 = _find_free_zz(ui, items4)
    check("某格按钮文字全丢时 -> 不盲点（返回 None）", r4 is None and r4 is not False,
          repr(r4))

    # ⑤ 实测场景：格子1「20/强征」（花虎符），格子2/3「等待中」 -> 返回 False
    items5 = [
        it("征收", 350, 785), it("征收", 870, 785), it("征收", 1390, 785),
        it("5000", 350, 800), it("5000", 870, 800), it("5000", 1390, 800),
        it("20/强征", 235, 877), it("等待中···", 800, 887), it("等待中···", 1320, 887),
    ]
    r5 = _find_free_zz(ui, items5)
    check("「20/强征」+「等待中」-> 返回 False（付费，别碰）", r5 is False, repr(r5))

    # ⑥ 只用颜色也要认得出来：没有文字，但有「强征」的红字+白底
    items6 = [it("征收", 350, 785)]
    check("纯 OCR 无字时不当成 free", _find_free_zz(ui, items6) is None)

    # ⑦ 「强征」的各种 OCR 变体都不能被当成免费
    for bad in ("20/强征", "20/強征", "强征", "弓虽征", "20/征收", "立即征收"):
        check("「%s」不算免费按钮" % bad, not _is_free_zz_txt(bad), repr(bad))
    for good in ("征收", "征丬攵", "氵正收", "征収"):
        check("「%s」算免费按钮" % good, _is_free_zz_txt(good), repr(good))


# --------------------------------------------------------------------------- 3. 特性按钮

def test_texing():
    print("\n[3] 特性面板：免费才抽，付费不抽")
    ui = FakeUi()
    # 免费态：按钮「获取1张」+ 左边「免费」标签
    items_free = [
        it("剩余特性13/15", 1516, 524),
        it("免费次数1", 1477, 737),
        it("免费", 1380, 820),
        it("获取1张", 1480, 824),
    ]
    cand = _pick_free_button(ui, items_free)
    check("免费态选中「获取1张」", cand is not None and norm(cand.text) == "获取1张", repr(cand))
    tag = ui.near(items_free, "免费", cand, dx=320, dy=110) if cand else None
    check("免费态能找到旁边的「免费」标签", tag is not None, repr(tag))

    # 付费态：只有「立鼠获取」+ 100 玉符 + 倒计时
    items_paid = [
        it("剩余特性13/15", 1516, 524),
        it("17:17:24后可免费获取1张", 1400, 616),
        it("半价次数5", 1509, 628),
        it("立鼠获取", 1480, 812),
        it("100", 1512, 844),
    ]
    cand2 = _pick_free_button(ui, items_paid)
    check("付费态选不出免费按钮", cand2 is None, repr(cand2))
    left = _parse_free_countdown(items_paid)
    check("能解析出倒计时秒数", left == 17 * 3600 + 17 * 60 + 24, repr(left))

    # 倒计时文案不能被当成按钮
    only_cd = [it("17:17:24后可免费获取1张", 1400, 616)]
    check("倒计时文案不会被当成按钮", _pick_free_button(ui, only_cd) is None)


# --------------------------------------------------------------------------- 4. 界面判定

def test_state():
    print("\n[4] 界面判定：招募卡包详情页不能当成主城")
    ui = FakeUi()
    home = [
        it("云魇丨奈子", 376, 19), it("势力值0", 359, 49), it("土地Lv.3", 713, 569),
        it("出征", 1194, 701), it("计略", 1128, 802), it("招募", 1807, 1038),
    ]
    check("主城判定为 home", ui.is_home(home) is True)

    pack = [
        it("招募28次内，必获赠5星武将", 960, 176),
        it("招募1次", 1431, 507), it("招募5次", 1431, 649),
        it("心愿积分", 542, 797), it("额外赠送35", 820, 850),
    ]
    check("卡包详情页不算主城", ui.is_home(pack) is False)

    recruit_list = [it("招募", 113, 54), it("赛季卡包", 506, 144),
                    it("免费次数1", 1683, 915)]
    check("招募列表页不算主城", ui.is_home(recruit_list) is False)


# --------------------------------------------------------------------------- 5. 多帧合并

def test_merge():
    print("\n[5] 多帧 OCR 合并（特性面板单帧会糊）")
    frame_ok = [it("剩余特性13/15", 1516, 524), it("获取1张", 1480, 824)]
    frame_bad = [it("0剩特鞋13的5", 1516, 524), it("后司费取长", 1566, 585)]
    merged = merge_items([frame_bad, frame_ok])
    texts = [norm(m.text) for m in merged]
    # 注意 texts 是归一化过的（norm 会丢掉 "/"），断言两边都要归一化
    check("合并后保留好的那帧", norm("剩余特性13/15") in texts, str(texts))
    check("合并后仍能匹配到锚点", find_text(merged, "剩余特性") is not None)


# --------------------------------------------------------------------------- 6. 价格读取

def test_price():
    print("\n[6] 招募价格：半价 100 / 原价 200 要能区分")
    btn = it("招募1次", 729, 910, 130, 40)
    for price in (100, 200, 950):
        items = [it(str(price), 620, 910, 90, 40), btn]
        got = _price_left_of(items, btn)
        check("读到价格 %d" % price, got == price, repr(got))


def test_stamp():
    print("\n[7] 「已征收」红章：斜盖章 OCR 读不出，只能靠颜色认")
    import cv2
    import numpy as np
    img = np.zeros((1080, 1920, 3), dtype=np.uint8)
    red = (60, 60, 230)                      # 亮红（BGR）

    # 槽1：斜盖章，实测外接框 141x78
    img[855:855 + 78, 427 - 70:427 - 70 + 141] = red
    # 槽3：付费按钮上「征收」两个小红字，实测 22x30 —— 面积够但太窄，不能算盖章
    img[866:866 + 30, 1467 - 11:1467 - 11 + 22] = red

    check("又大又宽的亮红块 -> 判为已征收", _has_done_stamp(img, 427) is True)
    check("窄小的红字     -> 不判为已征收", _has_done_stamp(img, 1467) is False)
    check("没有截图时     -> 不误判", _has_done_stamp(None, 427) is False)

    blank = np.zeros((1080, 1920, 3), dtype=np.uint8)
    check("空面板         -> 没有红章", _has_done_stamp(blank, 427) is False)


def test_neizheng_not_sub():
    print("\n[8] 内政主界面的入口字，不能被当成「已经在子面板里了」")
    # 这组文字取自 2026-09-18 实测截图 tx_panel_3_067.png：
    # 当时内政主界面上的入口「特性」被 OCR 读成「犄性」，锚点「特鞋」被删成单字
    # 「特」，成了「特性」的子串 —— 于是脚本以为「已经在特性面板里」，
    # 直接跳过、根本没抽那一次免费特性。
    ui = FakeUi()
    neizheng = [
        it("云魇丨奈子", 376, 19), it("政策", 594, 397), it("犄性", 1640, 445),
        it("橈收", 1755, 590), it("子弟", 779, 521), it("市井", 1045, 690),
        it("演武", 1525, 658), it("政务", 1276, 738), it("荣誉786", 110, 800),
        it("立即征收2/3", 1422, 592),
    ]
    check("内政主界面不算「特性」面板", ui._in_panel("特性", neizheng) is False)
    check("内政主界面不算「税收」面板", ui._in_panel("税收", neizheng) is False)
    check("内政主界面不算「市井」面板", ui._in_panel("市井", neizheng) is False)
    check("内政主界面仍能被认成内政", ui.is_neizheng(neizheng) is True)

    # 反过来：真正的特性面板必须认得出来
    tx = [it("剩余特性13/15", 1516, 524), it("免费次数1", 1477, 737),
          it("免费", 1380, 820), it("获取1张", 1480, 824)]
    check("真特性面板要认得出", ui._in_panel("特性", tx) is True)


def test_gongpin():
    print("\n[9] 贡品礼包：物品说明浮层不能被当成「领取成功」")
    ui = FakeUi()
    # 2026-09-18 实测：点「每日领取」图标弹出的是「玉符」说明浮层，老代码把它
    # 误判成领取成功，于是每天都会打印一个假的「✓ 完成」。
    tooltip = [
        it("玉符", 300, 260), it("当前拥有数量:639", 380, 300),
        it("货币，可1:1购买虎符、将令等", 420, 350),
        it("只能通过充值获得，无法交易", 420, 390),
        it("消耗玉符时，优先消耗普通玉符", 420, 430),
        it("每日领取", 1171, 604), it("150", 1171, 730),
        it("立即获得", 952, 604), it("300", 952, 730),
        it("¥30/续期", 1060, 896),
    ]
    check("玉符说明浮层 -> 不算领取成功", _got_reward(ui, tooltip) is False)
    check("真奖励弹窗(带确定) -> 认得出",
          _got_reward(ui, [it("恭喜获得", 960, 400),
                           it("玉符x150", 960, 460),
                           it("确定", 960, 600)]) is True)


def test_blacklist():
    print("\n[10] 安全黑名单：拦得住花钱按钮，又不能误伤正常导航")
    from stzb.config import load as load_cfg
    from stzb.ui import Ui as RealUi
    ui = RealUi(None, load_cfg())
    check("「¥30/续期」会被拦下", ui.safe_click_guard("¥30/续期", ("续期",)) is False)
    check("「支付」会被拦下", ui.safe_click_guard("支付", ("支付",)) is False)
    # 关键回归：「充值好礼」是贡品礼包必经的页签，加黑名单时很容易被「充值」误伤
    check("「充值好礼」页签不被误拦",
          ui.safe_click_guard("充值好礼", ("充值好礼",)) is True)
    check("「贡品礼包」不被误拦",
          ui.safe_click_guard("贡品礼包", ("贡品礼包",)) is True)


def test_info_popup():
    print("\n[11] 信息弹窗：「名望升级/解锁新功能」要能自动关掉")
    ui = FakeUi()
    ui.tapped = []
    ui.log = lambda *a: None
    ui.tap = lambda x, y: ui.tapped.append((x, y))
    # 2026-09-18 实测：这个弹窗不带「取消/跳过/关闭」，只有居中的「确定」，
    # 老代码会一直「等待中…」直到 300 秒启动超时 —— 就是用户说的「卡壳」。
    popup = [
        it("名望升级", 960, 124), it("解锁新功能", 960, 508),
        it("获得奖励", 1010, 508), it("扫荡", 427, 793), it("练兵", 641, 794),
        it("巡察", 855, 793), it("屯田", 1069, 794), it("开发", 1282, 794),
        it("开垦", 1497, 794), it("确定", 909, 846),
    ]
    sig = ui.guard(popup)
    check("识别为 info_popup", sig == "info_popup", repr(sig))
    check("点了「确定」", len(ui.tapped) == 1 and ui.tapped[0] == (959, 861),
          str(ui.tapped))

    # 安全阀：带消费字样的居中「确定」绝不点（例如买虎符的确认键）
    ui.tapped = []
    danger = [it("虎符不足", 960, 400), it("100/购买", 1177, 711),
              it("确定", 960, 861)]
    check("带「购买/虎符」的弹窗不动手", ui.close_info_popup(danger) is False)
    check("危险弹窗没有发生点击", ui.tapped == [], str(ui.tapped))

    # 反向：没有居中确认键的界面不能被误判成弹窗
    check("普通内政界面不算弹窗",
          ui.close_info_popup([it("政策", 540, 350), it("税收", 1767, 535)]) is False)


def test_kw_point():
    print("\n[12] OCR 连读框：中心点不能直接当按钮位置")
    from stzb.ui import Ui as RealUi
    # 2026-09-18 实测：主城顶栏被读成「活动36小时」，框 (273,164)-(441,187)，
    # 框中心 (368,175) 落在隔壁 36 小时 buff 图标上 —— 点下去开的是「初出茅庐」，
    # 面板一盖，贡品礼包整个任务就废了。
    hit = TextItem("活动36小时", (273, 164, 441, 187), (368, 175))
    p = RealUi._kw_point(hit, ("活动",))
    check("「活动36小时」取左侧「活动」那一段 -> %s" % (p,), p[0] < 330 and p[1] == 175,
          "期望 x<330（活动大约在 273~350），实得 %s" % (p,))

    # 完全相等的框不受影响
    hit2 = TextItem("活动", (273, 164, 350, 187), (311, 175))
    check("精确命中时保持原中心",
          RealUi._kw_point(hit2, ("活动",)) == (311, 175),
          str(RealUi._kw_point(hit2, ("活动",))))

    # 非前缀关系（美术字体误读）不能乱切：整体框中心就是它自己的位置
    hit3 = TextItem("橈叫攵", (1700, 500, 1830, 530), (1765, 515))
    check("非前缀命中保持原中心", RealUi._kw_point(hit3, ("税收", "橈叫攵")) == (1765, 515),
          str(RealUi._kw_point(hit3, ("税收", "橈叫攵"))))

    # 「立即获得300」这类带数值的说明块：关键词「立即获得」(4字) 占整框(7字)的 4/7，
    # 取左段中点 = 860 + 180*(4/7)/2 ≈ 911，落在「立即获得」文字上、避开「300」
    hit4 = TextItem("立即获得300", (860, 600, 1040, 630), (950, 615))
    p4 = RealUi._kw_point(hit4, ("立即获得",))
    check("「立即获得300」取左侧段 -> %s" % (p4,), p4[0] < 940, str(p4))


def test_main_city_tooltip():
    print("\n[13] 主城的资源说明浮层，不能冒充「内政」界面")
    # 2026-09-19 实测：在主城点一下资源数字会弹出「铜钱」说明浮层，里面写着
    # 「内政税收 · 免费征收」「前往市井」——凑齐 2 个锚点，于是 is_neizheng 返回 True，
    # open_neizheng 一个点击都没发出去就返回成功，后续所有操作全部错位
    # （下一个点击落在了浮层的「任务-主要事宜」上，点开了任务面板）。
    ui = FakeUi()
    city_with_tip = [
        it("招募", 1807, 1030), it("出征", 1194, 701), it("计略", 1128, 802),
        it("土地Lv.3", 713, 569), it("势力值0", 359, 49),
        it("铜钱", 1600, 80), it("当前拥有数量:90000", 1650, 140),
        it("内政税收", 1479, 367), it("免费征收", 1560, 390),
        it("前往市井", 1500, 620),
    ]
    check("主城+资源浮层不算内政界面", ui.is_neizheng(city_with_tip) is False)
    check("主城+资源浮层不算「税收」面板",
          ui._in_panel("税收", city_with_tip) is False)
    check("主城+资源浮层仍认得出主城", ui.is_home(city_with_tip) is True)

    # 内政主界面：OCR 只读得到几行，is_neizheng 凑不够锚点，但「立即征收N/3」
    # 这个入口状态标签必须足以认出它
    thin = [it("一立即征收1/3", 1730, 774), it("01:59:50", 1766, 626)]
    check("只有 2 行文字时也能认出内政主界面",
          ui._is_neizheng_screen(thin) is True)
    check("内政主界面不算「税收」面板（薄 OCR 版）",
          ui._in_panel("税收", thin) is False)


def test_shijing():
    print("\n[14] 市井：按钮白块 + 币种判定（老代码把玉符价当铜钱价点下去）")
    ui = FakeUi()
    img = _blank()
    # 2 列 3 行，和实测布局一致：免费 / 玉符 / 铜钱
    b_free = _btn(img, 750, 294)                       # 「免费」按钮
    _ink(img, 750, 294)
    b_jade = _btn(img, 1663, 318)
    _ink(img, 1663, 318)
    _jade(img, 1700, 260)                              # 玉符价图标在按钮正上方
    b_cop = _btn(img, 750, 449)
    _ink(img, 750, 449)                                # 铜钱价没有绿色
    blocks = _shop_blocks(img)
    check("能识别出 3 个按钮白块（得到 %d 个）" % len(blocks), len(blocks) == 3,
          str(blocks))

    items = [
        it("材料", 205, 274), it("乌钢", 205, 325),
        it("材料", 1117, 264), it("梧桐木", 1117, 301),
        it("材料", 205, 395), it("青铜", 205, 432),
        it("．原价：30．015", 1571, 264), it("《、25000", 797, 401),
        it("购买", 1710, 326), it("购买", 797, 458),
    ]
    free_blocks = [b for b in blocks
                   if not _block_labels(items, b) and _block_has_ink(img, b)]
    check("「免费」按钮 = 块上没有 OCR 文字（得到 %d 个）" % len(free_blocks),
          len(free_blocks) == 1 and free_blocks[0] == b_free, str(free_blocks))
    check("「免费」按钮里确实有字（不是空白块）", _block_has_ink(img, b_free))
    check("玉符价：绿占比 %.3f >= 阈值" % _band_jade_frac(img, b_jade),
          _band_jade_frac(img, b_jade) >= 0.02)
    check("铜钱价：绿占比 %.3f < 阈值" % _band_jade_frac(img, b_cop),
          _band_jade_frac(img, b_cop) < 0.02)

    # 材料名 → 同一行右边的按钮块
    hit = [x for x in items if x.text == "梧桐木"][0]
    b = _block_in_row(blocks, hit)
    check("「梧桐木」关联到右侧那个块", b == b_jade, str(b))
    check("「梧桐木」的价格带能读出数字", _band_price(items, b) == "30015",
          str(_band_price(items, b)))
    # 拿不到图时一律按「玉符」处理，绝不冒险买
    check("拿不到截图时保守判成玉符", _band_jade_frac(None, b_jade) >= 0.02)


def test_recruit_free_badge():
    print("\n[15] 招募：「免费」绿标签（这两个字 OCR 读不出来）")
    img = _blank()
    # 「招募1次」按钮中心 (909,504)；实测绿标签中心在它左侧 106px、下方 23px
    img[511:543, 783:823] = (60, 200, 80)              # 40x32 的绿标签
    btn1 = itc("招募1次", 909, 504)
    check("「招募1次」左侧有绿标签 -> 判定免费", _has_free_badge(img, btn1) is True)
    btn5 = itc("招募5次", 909, 639)
    check("「招募5次」附近没有绿标签 -> 不判定免费",
          _has_free_badge(img, btn5) is False)
    check("没有截图时不会误判免费", _has_free_badge(None, btn1) is False)
    # 距离太远的绿块不能算：把标签挪到别处
    img2 = _blank()
    img2[511:543, 300:340] = (60, 200, 80)             # 离按钮很远
    check("远处的绿块不算免费标签", _has_free_badge(img2, btn1) is False)


def test_run_lock():
    print("\n[16] 单实例锁（定时任务和手动运行不能同时操控模拟器）")
    import datetime as _dt
    import json as _json
    import tempfile
    import run_daily as rd
    d = tempfile.mkdtemp()
    path = os.path.join(d, "run.lock")

    l1 = rd.RunLock(path, lambda m: None)
    check("第一次获取锁成功", l1.acquire() is True)
    check("锁文件已落盘", os.path.exists(path))
    l2 = rd.RunLock(path, lambda m: None)
    check("同进程再取锁被挡下", l2.acquire() is False)   # pid 还活着
    l1.release()
    check("释放后锁文件被删掉", not os.path.exists(path))

    # 残留锁（pid 已死 / 时间过期）要能自动接管
    with open(path, "w", encoding="utf-8") as f:
        _json.dump({"pid": 999999, "started": _dt.datetime.now().isoformat()}, f)
    l3 = rd.RunLock(path, lambda m: None)
    check("pid 已死的残留锁可以接管", l3.acquire() is True)
    l3.release()

    old = (_dt.datetime.now() - _dt.timedelta(hours=2)).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        _json.dump({"pid": os.getpid(), "started": old}, f)
    l4 = rd.RunLock(path, lambda m: None)
    check("超过 30 分钟的过期锁可以接管", l4.acquire() is True)
    l4.release()
    check("pid_alive 认得自己", rd.pid_alive(os.getpid()) is True)


def test_anchors():
    print("\n[17] 关面板锚点：主城顶栏一直挂着「荣誉」，不能拿它当市井面板的锚点")
    # 2026-09-19 实测：市井面板的锚点原来是 ("宝物商队","贡献","名师","荣誉")，
    # 但主城顶栏常年显示「荣誉 30/30」——于是关面板的循环永远等不到锚点消失，
    # 只能把 5 个候选坐标全点一遍（每个面板白花 30 秒），还会点到主城上的别的入口。
    ui = FakeUi()
    city = [it("招募", 1807, 1030), it("出征", 1194, 701), it("计略", 1128, 802),
            it("土地Lv.3", 713, 569), it("势力值0", 359, 49),
            it("荣誉", 552, 175), it("基金", 1063, 175), it("边下边玩", 1250, 180)]
    check("主城不算任何面板（锚点应为空）",
          ui._screen_anchors(city) == (), str(ui._screen_anchors(city)))
    sj = [it("市井", 201, 143), it("贡献", 480, 144), it("宝物商队", 755, 143),
          it("赛季", 1039, 144), it("荣誉", 1594, 144),
          it("材料", 205, 274), it("乌钢", 205, 325), it("剩余3", 205, 466)]
    a = ui._screen_anchors(sj)
    check("市井面板的锚点里不含「荣誉」", "荣誉" not in a, str(a))
    check("市井面板锚点锁定在「宝物商队」", a == ("宝物商队",), str(a))
    # 税收面板的锚点必须是面板专有词
    ss = [it("征收", 427, 784), it("征收中01:50:09", 1466, 702),
          it("已征收", 443, 785)]
    check("税收面板锚点齐全", "已征收" in ui._screen_anchors(ss),
          str(ui._screen_anchors(ss)))


def test_recruit_half_price():
    print("\n[18] 招募半价：竖排「打折」丝带 + 按钮正下方的价格")
    # 2026-09-19 实测（rc_free_panel_087.png）：
    #   「招募1次」按钮文字 box=(840,471)-(979,502)，白块 (790,459)-(1038,548)
    #   红色「打折」丝带 box=(784,458)-(824,543)  40x85 面积 2491 高宽比 2.12
    #   按钮里的价格红字「100」 16x22 面积 133 高宽比 1.38
    #   「打折」是白底红字竖排，整帧 OCR 里根本没有这两个字 → 只能靠颜色认。
    img = _blank()
    img[458:543, 784:824] = (40, 40, 210)          # 红丝带 40x85 → h/w=2.12
    btn1 = itc("招募1次", 909, 486, w=139, h=31)
    btn5 = itc("招募5次", 909, 639, w=145, h=31)
    check("按钮左边缘的红丝带 -> 认得出「打折」", _has_discount_ribbon(img, btn1) is True)
    check("「招募5次」没有丝带 -> 不认", _has_discount_ribbon(img, btn5) is False)
    check("没有截图时不会误判打折", _has_discount_ribbon(None, btn1) is False)

    # 只有按钮里的价格红字（面积 133、高宽比 1.38）时不能当成丝带
    img2 = _blank()
    img2[517:539, 925:941] = (40, 40, 210)         # 16x22 小红字
    img2[517:539, 943:959] = (40, 40, 210)
    check("按钮内的价格红字不会被当成打折丝带",
          _has_discount_ribbon(img2, btn1) is False)

    # 价格在按钮文字的正下方
    items = [itc("招募1次", 909, 486, w=139, h=31),
             itc("额外赠送，35", 931, 436),
             itc("@100", 913, 524, w=92, h=34),
             itc("招募5次", 909, 639, w=145, h=31),
             itc("0950", 913, 677, w=98, h=34)]
    check("能读到按钮正下方的价格 100", _price_under_btn(items, items[0]) == 100,
          str(_price_under_btn(items, items[0])))
    check("不会把「招募5次」的 950 当成本次价格",
          _price_under_btn(items, items[0]) != 950)
    check("不会把「额外赠送35」的 35 当成价格",
          _price_under_btn(items, items[0]) != 35)
    # 半价用完后按钮变成 200 → 必须被上限 100 挡下
    items2 = [itc("招募1次", 909, 486, w=139, h=31), itc("200", 913, 524, w=60, h=34)]
    check("原价 200 会被读出来（好让上面的上限检查拦掉）",
          _price_under_btn(items2, items2[0]) == 200,
          str(_price_under_btn(items2, items2[0])))

    # 「虎符不足」兑换弹窗（文字抄自真机截图 rc_half_after_010.png）
    dlg = [itc("虎符不足", 960, 339), itc("100", 962, 504),
           itc("还需要100虎符才能进行本次操作", 958, 556),
           itc("！，兑换比例1玉符=1虎符", 966, 634),
           itc("批量购买", 760, 710), itc("100/购买", 1205, 709)]
    ok, why = _hufu_exchange_ok(dlg, 100)
    check("真实兑换弹窗 -> 确认可以点购买", ok is True, why)

    ui = FakeUi()
    pt = _find_hufu_buy(ui, dlg, 100)
    check("兑换弹窗里选「100/购买」而不是「批量购买」", pt == (1205, 709), str(pt))

    # 混进充值字样就不许点
    ok2, why2 = _hufu_exchange_ok(dlg[:3] + [itc("立即充值", 960, 700)], 100)
    check("弹窗里有「充值」-> 放弃", ok2 is False, why2)

    # 什么都不像（普通确认窗）也不许点
    ok3, why3 = _hufu_exchange_ok([itc("确定", 960, 700), itc("取消", 760, 700)], 100)
    check("认不出是兑换弹窗 -> 放弃", ok3 is False, why3)

    # 兑换比例不划算（付 3 只换 1）-> 放弃
    ok4, why4 = _hufu_exchange_ok([itc("还需要300虎符才能进行本次操作", 958, 556),
                                   itc("兑换比例3玉符=1虎符", 966, 634)], 100)
    check("兑换比例不划算(3换1) -> 放弃", ok4 is False, why4)

    # 超过上限的价格读不出来时，_find_hufu_buy 不该选那个按钮
    dlg_hi = [itc("虎符不足", 960, 339), itc("200/购买", 1205, 709)]
    check("价格超上限的兑换键选不出来", _find_hufu_buy(ui, dlg_hi, 100) is None)


def test_yanwu():
    print("\n[19] 演武扫荡奖励：面板 → 弹窗 → 冷却判断（2026-09-19 实机抄下来的坐标）")
    ui = FakeUi()
    ui.tapped = []
    ui.log = lambda *a: None
    ui.tap = lambda x, y: ui.tapped.append((x, y))
    ui.never_tap = []

    # ① 演武面板本身（真机 OCR 原文）。注意「扫」被读成「归」。
    panel = [
        itc("Lv.5", 239, 872, w=60, h=25),
        itc("500", 119, 925, w=60, h=25),
        itc("=同步主城队伍", 286, 980, w=140, h=30),
        itc("日剩余失败次数27〕'0氵《'。", 1053, 918, w=260),
        itc("挑战", 957, 977, w=60),
        itc("归荡奖励", 1759, 981, w=100),
        itc("22：25：02后可领取", 1758, 1017, w=200),
    ]
    # ②「扫荡演武奖励」弹窗（真机 OCR 原文）
    dialog = [
        itc("扫荡演-", 951, 111, w=200),
        itc("演武挑战至2一4为止的扫荡奖励", 959, 244, w=400),
        itc("6800", 445, 434, w=70), itc("1木材", 448, 482, w=70),
        itc("6800", 646, 433, w=70), itc("1铁矿", 650, 482, w=70),
        itc("6800", 849, 434, w=70), itc("1粮草", 852, 483, w=70),
        itc("6800", 1051, 434, w=70), itc("1石料", 1054, 482, w=70),
        itc("1070", 1254, 434, w=70), itc("铜钱", 1254, 482, w=80),
        itc("20", 1455, 434, w=50), itc("战法经验", 1456, 482, w=100),
        itc("0", 749, 585, w=40), itc("20", 748, 634, w=40),
        itc("虎符", 749, 682, w=70),
        itc("16", 951, 634, w=40), itc("属性阅历", 951, 682, w=100),
        itc("0", 1143, 585, w=40), itc("战法阅历", 1153, 682, w=100),
        itc("22:22:37后可领取", 959, 836, w=220),
    ]

    # —— 弹窗判据：只看「扫荡奖励」四个字不行，面板右下角本来就有这四个字
    check("演武面板本身不算奖励弹窗", _is_sweep_dialog(ui, panel) is False)
    check("奖励弹窗识别成功（9 种奖励名认出 %d 个）"
          % sum(1 for k in ("木材", "铁矿", "粮草", "石料", "铜钱",
                            "战法经验", "虎符", "属性阅历", "战法阅历")
                if ui.has(dialog, k)),
          _is_sweep_dialog(ui, dialog) is True)
    # 内政主界面 + 一张只有「扫荡奖励」字样的浮层，不能误判成弹窗
    check("没有奖励名的那种浮层不算弹窗",
          _is_sweep_dialog(ui, [itc("扫荡奖励", 960, 500, w=120)]) is False)

    # —— 倒计时：全角/半角冒号都要认；奖励数字（6800/1070/20）不能误伤
    check("半角倒计时读到了：%r" % _sweep_countdown(dialog),
          _sweep_countdown(dialog) == "22:22:37后可领取")
    check("全角倒计时也认（22：25：02）",
          _sweep_countdown(panel) == "22：25：02后可领取",
          repr(_sweep_countdown(panel)))
    check("奖励数字不会被当成倒计时",
          _sweep_countdown([x for x in dialog if "后可领取" not in x.text]) is None)
    check("_parse_clock('22:22:37后可领取') == 22*3600+22*60+37",
          _parse_clock("22:22:37后可领取") == 80557,
          str(_parse_clock("22:22:37后可领取")))

    # —— 冷却中：底部没有「领取」，所以绝不能点
    check("冷却中 -> 找不到「领取」按钮", _sweep_claim_button(ui, dialog) is None)

    # —— 可领取：底部那条变成「领取」
    claimable = [x for x in dialog if "后可领取" not in x.text] \
        + [itc("领取", 960, 838, w=120)]
    btn = _sweep_claim_button(ui, claimable)
    check("可领取时命中底部「领取」按钮 @%s" % (btn.center if btn else None,),
          btn is not None and btn.center == (960, 838))
    check("可领取时没有倒计时", _sweep_countdown(claimable) is None)

    # —— 入口：右下角那块；弹窗副标题里的「…的扫荡奖励」不能当入口
    e = _find_sweep_entry(ui, panel)
    check("右下角「归荡奖励」被认成入口 @%s" % (e.center if e else None,),
          e is not None and e.center == (1759, 981))
    check("弹窗副标题不算入口（不在右下角）",
          _find_sweep_entry(ui, dialog) is None)

    # —— 进出面板：内政主界面上有「演武」入口，不能被当成「已经在演武面板里」
    nz = [itc("政策", 554, 344, w=80), itc("犄性", 1640, 439, w=80),
          itc("演武", 1527, 650, w=80), itc("橈叫攵", 1770, 604, w=90),
          itc("政务", 1284, 738, w=80), itc("市集", 545, 785, w=80)]
    check("演武面板 = 已在「演武」里", ui._in_panel("演武", panel) is True)
    check("内政主界面上的「演武」入口不算已进入",
          ui._in_panel("演武", nz) is False)
    check("演武面板不会被误判成税收/市井面板",
          ui._in_panel("税收", panel) is False
          and ui._in_panel("市井", panel) is False)
    check("演武面板锚点含「同步主城队伍」",
          "同步主城队伍" in ui._screen_anchors(panel),
          str(ui._screen_anchors(panel)))
    check("奖励弹窗盖住面板时锚点为空（走右上 ✕ 关）",
          ui._screen_anchors(dialog) == (), str(ui._screen_anchors(dialog)))

    # —— 宝箱金色块定位（真机连拍 5 帧都是 (1718,874)-(1799,938)，面积 2917）
    img = _blank()
    img[874:938, 1718:1799] = (60, 190, 235)          # 金色宝箱（BGR）
    img[820:861, 1530:1725] = (60, 190, 235)          # 干扰：背景里的金色山岩（又宽又扁）
    check("宝箱定位到中心 (1758,906)：得到 %s" % (_find_sweep_icon(img),),
          _find_sweep_icon(img) == (1758, 906))
    check("空白图 -> None", _find_sweep_icon(_blank()) is None)
    check("img=None 安全返回 None", _find_sweep_icon(None) is None)
    # 只有扁扁一条金色（山岩）时不能硬认成宝箱
    img2 = _blank()
    img2[840:861, 1660:1860] = (60, 190, 235)
    check("又宽又扁的金色条不算宝箱", _find_sweep_icon(img2) is None,
          str(_find_sweep_icon(img2)))

    # —— 安全阀：弹窗里有「虎符」，绝不能被「点确定」的信息弹窗逻辑接管
    ui.tapped = []
    with_ok = dialog + [itc("确定", 960, 838)]
    check("含「虎符」的奖励弹窗不给 close_info_popup 动",
          ui.close_info_popup(with_ok) is False)
    check("该弹窗没有发生任何点击", ui.tapped == [], str(ui.tapped))


def test_yanwu_not_home():
    print("\n[20] 演武面板 + 顶部公告栏 ≠ 主城（is_home 误判 + 「跳过战斗动画」被当关闭键）")
    ui = FakeUi()
    ui.log = lambda *a: None
    ui.never_tap = []
    ui.tapped = []
    ui.tap = lambda x, y, delay=0.7: ui.tapped.append((x, y))

    # 真机帧 sub_re_演武_1_0_013.png 的原文：演武面板 + 顶部滚动公告
    # 「恭喜xx招募到5星武将」。老 is_home 看到「土地」+「招募」就返回 True。
    panel = [
        itc("Lv．4土地", 1503, 443), itc("守军难度", 1500, 477),
        itc("=同步主城队伍", 286, 980, w=140), itc("500", 119, 925),
        itc("Lv.5", 239, 872), itc("归荡奖励", 1759, 981, w=100),
        itc("恭喜不喝可乐丷", 500, 30, w=160),
        itc("招募到5星武将(汉", 700, 60, w=200),
    ]
    check("演武面板不算主城", ui.is_home(panel) is False)
    check("演武面板不算内政界面", ui.is_neizheng(panel) is False)
    check("演武面板仍然认得出「演武」",
          ui._in_panel("演武", panel) is True)

    # 真主城（同样挂着招募公告）必须仍然算主城
    city = [
        itc("招募", 1807, 1030), itc("出征", 1194, 701), itc("土地Lv.3", 713, 569),
        itc("势力值0", 359, 49), itc("计略", 1085, 802),
        itc("恭喜不喝可乐丷", 500, 30, w=160),
        itc("招募到5星武将(汉", 700, 60, w=200),
    ]
    check("带招募公告的真主城仍算主城", ui.is_home(city) is True)

    # to_home 里「跳过战斗动画」是复选框，不是关面板键 —— 实测被连点 5 次
    # （白花 20 秒，还把复选框来回切）
    ui.ocr = lambda tag="x": (panel, "fake.png")
    ui.to_home(max_rounds=1)
    bad = [p for p in ui.tapped if 1200 < p[0] < 1450 and 950 < p[1] < 1060]
    check("to_home 不去点「跳过战斗动画」复选框", bad == [], str(bad))
    check("to_home 确实去点了右上角的 ✕ 候选（%s）" % (ui.tapped[:2],),
          len(ui.tapped) > 0 and any(p[0] > 1800 for p in ui.tapped))

    # 认不出的界面（锚点为空，实测是「政务」面板）：必须把 ✕ 候选全试一遍，
    # 老代码只点 cands[0]=(1832,53)，而政务的 ✕ 在 (1778,155) → 300 秒启动超时。
    ui.tapped = []
    unknown = [itc("常努", 240, 245, w=80), itc("要务", 445, 245, w=80),
               itc("募兵", 585, 880, w=80), itc("实仓", 960, 880, w=80),
               itc("立刻获得预备兵", 585, 800, w=160)]
    ui.ocr = lambda tag="x": (unknown, "fake.png")
    ui.to_home(max_rounds=1)
    check("认不出的界面 -> 把右上角 ✕ 候选试了一串（试了 %d 个 %s）"
          % (len(ui.tapped), ui.tapped), len(ui.tapped) >= 3, str(ui.tapped))


def test_emulator():
    """MuMu 状态解析。不碰真模拟器：把 MuMuManager 的调用换成假数据。"""
    import json as _json
    from stzb.emulator import MuMu

    print("\n[21] 模拟器状态解析（MuMuManager 的 info 返回）")
    calls = {}

    def fake(m, payload):
        def _run(args, timeout=90.0):
            calls["args"] = list(args)
            return 0, payload
        m._run = _run
        return m

    base = {"is_process_started": True, "is_android_started": True,
            "player_state": "start_finished", "pid": 5428, "adb_port": 16384}

    m = fake(MuMu(manager="x", logger=lambda s: None), _json.dumps(base))
    check("启动完成 -> is_running / is_ready 都 True",
          m.is_running() and m.is_ready())

    m = fake(MuMu(manager="x", logger=lambda s: None),
             _json.dumps(dict(base, is_android_started=False, player_state="starting")))
    check("Android 还没起来 -> is_running True 但 is_ready False",
          m.is_running() and not m.is_ready())

    m = fake(MuMu(manager="x", logger=lambda s: None),
             _json.dumps(dict(base, is_android_started=False)))
    check("state 说好了但 android False -> 不算就绪", m.is_ready() is False)

    m = fake(MuMu(manager="x", logger=lambda s: None),
             _json.dumps(dict(base, is_process_started=False, player_state="shutdown")))
    check("已关机 -> is_running False", m.is_running() is False)

    m = fake(MuMu(manager="x", logger=lambda s: None), "这不是 JSON")
    check("返回非 JSON -> info() 为 None 且不抛异常",
          m.info() is None and m.is_running() is False and m.is_ready() is False)

    check("vmindex / manager 路径拼进命令",
          calls.get("args") == ["info", "-v", "0"], str(calls.get("args")))

    check("MuMuManager 路径不存在时 available() 为 False",
          MuMu(manager=r"C:\no\such\MuMuManager.exe",
               logger=lambda s: None).available() is False)

    # adb 侧：候选顺序优先；候选之外只认「形状像模拟器」的（见下一条）
    m = MuMu(manager="x", logger=lambda s: None,
             serial_candidates=["127.0.0.1:7555", "127.0.0.1:16384", "emulator-5554"])
    m.adb_serials = lambda: ["emulator-5554", "127.0.0.1:7555"]
    check("多个在线设备时按候选顺序取（实测 7555 和 emulator-5554 是同一台）",
          m.adb_online() == "127.0.0.1:7555")

    # ★ 2026-09-21：退路必须**排除 USB 实体机**。原来这里是「返回任意一个在线设备」，
    #   结果插着手机时 start_emulator 会把手机的 serial 当成模拟器报给后端
    #   （实测 340436524100AJ8），后端以为「模拟器已就绪」，其实根本没起来。
    m.adb_serials = lambda: ["192.168.1.9:5555"]
    check("陌生设备但形状像模拟器（host:port）→ 仍可先用着",
          m.adb_online() == "192.168.1.9:5555")

    m.adb_serials = lambda: ["340436524100AJ8"]
    check("★ USB 实体机的 serial 不能被当成模拟器（实测踩过的坑）",
          m.adb_online() is None)

    m.adb_serials = lambda: ["340436524100AJ8", "emulator-5554"]
    check("实体机 + 模拟器同时在 → 只认模拟器那个",
          m.adb_online() == "emulator-5554")

    m.adb_serials = lambda: []
    check("一台都没有时返回 None", m.adb_online() is None)


def test_report():
    """报告生成：挑图规则、截图归属、HTML 内嵌、latest.* 落盘。"""
    import json as _json
    import shutil
    import tempfile

    import numpy as np

    from stzb.core import write_png
    from stzb.report import RunReport, STATUS_ERROR, STATUS_OK, STATUS_SKIP, pick_shots

    print("\n[22] 执行报告（挑图 / 截图归属 / HTML 内嵌 / latest 指针）")

    tmp = tempfile.mkdtemp(prefix="stzb_report_")
    shots = os.path.join(tmp, "shots")
    os.makedirs(shots, exist_ok=True)
    try:
        # ---- pick_shots：优先 panel、必含最后一张、不超上限、保持时间序
        names = ["s_%02d.png" % i for i in range(1, 11)]
        names[4] = "ss_panel_05.png"                 # 第 5 张是面板全貌
        paths = [os.path.join(shots, n) for n in names]
        picked = pick_shots(paths, limit=5)
        check("挑图不超过上限（%d 张）" % len(picked), len(picked) <= 5, str(picked))
        check("必含 panel 那张", paths[4] in picked)
        check("必含最后一张（最终状态）", paths[-1] in picked)
        check("保持原有时间顺序",
              picked == sorted(picked, key=lambda p: paths.index(p)))
        check("张数少于上限时原样返回",
              pick_shots(paths[:3], limit=5) == paths[:3])

        # ---- 报告本体
        rep = RunReport(tmp, shots, slot="12:00", logger=lambda m: None)
        rep.note("ADB 已连接：127.0.0.1:7555")
        rep.env_info(模拟器="MuMu 12", ADB设备="127.0.0.1:7555")

        rep.skip("texing", "内政特性", "不在 00:00 档")

        rec = rep.begin("shijing", "内政市井")
        write_png(os.path.join(shots, "sj_panel_01.png"),
                  np.full((40, 60, 3), 200, dtype=np.uint8))
        write_png(os.path.join(shots, "sj_ok_02.png"),
                  np.full((40, 60, 3), 120, dtype=np.uint8))
        write_png(os.path.join(shots, "sj_ok_03.png.x2.png"),       # 中间产物，要滤掉
                  np.full((40, 60, 3), 90, dtype=np.uint8))
        rec("· 打开「内政」面板")
        rec("· 宝物商队免费已领")
        rep.finish(rec, True, seconds=12.5)

        rec = rep.begin("recruit", "招募")
        write_png(os.path.join(shots, "rc_fail_04.png"),
                  np.full((40, 60, 3), 30, dtype=np.uint8))
        rep.finish(rec, False, status=STATUS_ERROR, reason="异常：TimeoutError()",
                   seconds=3.2)

        check("统计：成功 1 / 失败 1 / 跳过 1",
              (rep.n_ok, rep.n_fail, rep.n_skip) == (1, 1, 1),
              str((rep.n_ok, rep.n_fail, rep.n_skip)))
        check("all_ok 为 False（有失败）", rep.all_ok is False)
        shijing = [e for e in rep.entries if e.key == "shijing"][0]
        check("截图按任务归属（市井 2 张，滤掉 .x2.png）",
              [os.path.basename(p) for p in shijing.shots] ==
              ["sj_panel_01.png", "sj_ok_02.png"], str(shijing.shots))
        check("失败任务的截图不会算到别人头上",
              [os.path.basename(p) for p in
               [e for e in rep.entries if e.key == "recruit"][0].shots] == ["rc_fail_04.png"])
        check("跳过的任务没有截图",
              [e for e in rep.entries if e.key == "texing"][0].shots == [])
        check("跳过的任务保留原因",
              [e for e in rep.entries if e.key == "texing"][0].reason == "不在 00:00 档")

        # 截图排序必须按文件名里的全局序号（拍摄顺序），不能按 mtime、更不能按文件名。
        # 这两个坑都真踩过：同一时钟刻度写的文件 mtime 完全相同；
        # 跨 tag 的字母序里 `sj_ok_02` 会排到 `sj_panel_01` 前面，序号过 100 后也乱。
        srt = os.path.join(tmp, "srt")
        os.makedirs(srt, exist_ok=True)
        rep2 = RunReport(tmp, srt, slot="00:00", logger=lambda m: None)
        rec2 = rep2.begin("k", "K")
        for n in ("cc_ok_02.png", "cc_panel_01.png", "cc_x_100.png", "cc_y_099.png"):
            write_png(os.path.join(srt, n), np.full((20, 30, 3), 150, dtype=np.uint8))
        rep2.finish(rec2, True, seconds=1.0)
        order = [os.path.basename(p) for p in rec2.entry.shots]
        check("跨 tag 时按序号排（panel_01 在 ok_02 之前）",
              order[:2] == ["cc_panel_01.png", "cc_ok_02.png"], str(order))
        check("序号过 100 也按数值排（099 在 100 之前，不是字母序）",
              order == ["cc_panel_01.png", "cc_ok_02.png", "cc_y_099.png", "cc_x_100.png"],
              str(order))
        # ★ 跨轮重名（2026-09-21 实测踩到）：
        #   截图文件名里的序号是 `Device` 的**进程内自增**计数器，每跑一轮都从 0 开始，
        #   所以**跨轮会重名**——同一个 tag 在同一位置拍出的名字两轮一模一样。
        #   而归属用的是「目录差集」；只比**文件名集合**的话，第二轮那张会被判成
        #   「本来就有」→ 该任务计 **0 张**。
        #   实测：手机第 5 轮「贡品」报「截图0张」，目录里却躺着 4 张新图 ——
        #   报告是排查的主要依据，把张数报成 0 等于把线索藏起来。
        rr = os.path.join(tmp, "rr")
        os.makedirs(rr, exist_ok=True)
        stale = os.path.join(rr, "gp_page_009.png")
        write_png(stale, np.full((20, 30, 3), 10, dtype=np.uint8))
        os.utime(stale, (1_600_000_000, 1_600_000_000))   # 上一轮留下的同名旧图
        rep4 = RunReport(tmp, rr, slot="12:00", logger=lambda m: None)
        rec4 = rep4.begin("gongpin", "贡品礼包")
        write_png(stale, np.full((20, 30, 3), 200, dtype=np.uint8))   # 同名、新内容
        write_png(os.path.join(rr, "gp_after_010.png"),
                  np.full((20, 30, 3), 200, dtype=np.uint8))
        rep4.finish(rec4, True, seconds=1.0)
        names4 = [os.path.basename(p) for p in rec4.entry.shots]
        check("跨轮同名的图仍算新增（不能因重名漏计）",
              names4 == ["gp_page_009.png", "gp_after_010.png"], str(names4))
        old_one = os.path.join(rr, "gp_old_001.png")
        write_png(old_one, np.full((20, 30, 3), 10, dtype=np.uint8))
        os.utime(old_one, (1_600_000_000, 1_600_000_000))
        rec5 = rep4.begin("x", "X")
        rep4.finish(rec5, True, seconds=1.0)
        check("没被重写的老图不算新增（不能把历史图全算进来）",
              rec5.entry.shots == [], str(rec5.entry.shots))

        # 制造 mtime 完全相同的情况，序号排序仍必须正确
        same = os.path.join(tmp, "same")
        os.makedirs(same, exist_ok=True)
        rep3 = RunReport(tmp, same, slot="00:00", logger=lambda m: None)
        rec3 = rep3.begin("k", "K")
        for n in ("z_ok_02.png", "a_panel_01.png"):
            write_png(os.path.join(same, n), np.full((20, 30, 3), 150, dtype=np.uint8))
        rep3.finish(rec3, True, seconds=1.0)
        check("mtime 相同时仍按序号排（不退回按文件名）",
              [os.path.basename(p) for p in rec3.entry.shots] ==
              ["a_panel_01.png", "z_ok_02.png"],
              str([os.path.basename(p) for p in rec3.entry.shots]))
        check("任务日志被收进报告（%d 行）" % len(shijing.notes), len(shijing.notes) == 2)
        check("summary_text 含档位与统计",
              "12:00" in rep.summary_text() and "成功 1" in rep.summary_text())

        rep.finished_at = rep.started_at
        paths = rep.write_all(os.path.join(tmp, "reports"), keep_days=14)
        check("HTML 报告已生成", os.path.exists(paths["html"]))
        check("latest.json / latest.md 已生成",
              os.path.exists(paths["latest_json"]) and os.path.exists(paths["latest_md"]))
        check("latest.html 已生成（README / 播报都引用这个固定路径）",
              bool(paths["latest_html"]) and os.path.exists(paths["latest_html"]))
        with open(paths["latest_json"], encoding="utf-8") as f:
            d = _json.load(f)
        check("latest.json 可解析且计数正确",
              d["counts"] == {"ok": 1, "fail": 1, "skip": 1}
              and d["slot"] == "12:00" and len(d["tasks"]) == 3, str(d.get("counts")))

        with open(paths["html"], encoding="utf-8") as f:
            page = f.read()
        check("HTML 含任务名", "内政市井" in page and "招募" in page)
        check("截图以 base64 内嵌（不是外链）",
              "data:image/jpeg;base64," in page and 'src="sj_panel_01.png"' not in page)
        check("HTML 里带失败原因", "TimeoutError" in page)
        check("HTML 是自包含的（没有引用本地 css/js）",
              "<style>" in page and "<link" not in page and "<script" not in page)
        check("latest.html 与本次报告内容一致",
              open(paths["latest_html"], encoding="utf-8").read() == page)

        # ---- 过期报告清理：只动 run_* 前缀
        old = os.path.join(tmp, "reports", "run_2020-01-01_000000.html")
        with open(old, "w", encoding="utf-8") as f:
            f.write("x")
        os.utime(old, (1000000, 1000000))
        keep = os.path.join(tmp, "reports", "manual_keep.html")
        with open(keep, "w", encoding="utf-8") as f:
            f.write("x")
        os.utime(keep, (1000000, 1000000))
        rep.write_all(os.path.join(tmp, "reports"), keep_days=14)
        check("过期 run_* 报告被清理", not os.path.exists(old))
        check("非 run_ 前缀的文件不动（保持不动别人的东西）", os.path.exists(keep))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_remote_config():
    """远端配置下发的安全边界 —— 这条错了会把人锁在门外，必须守住。"""
    import json as _json
    import shutil
    import tempfile

    from stzb.remote_config import (apply_remote, deep_merge, diff_summary,
                                    filter_payload, pick_job)

    print("\n[23] 远端配置下发（白名单 / 深合并 / 拉不到就不动本地）")

    payload = {
        "tasks": {"yanwu": False, "recruit": True},
        # 下面这些段**必须**被过滤掉
        "device": {"adb": r"D:\evil\adb.exe", "serial_candidates": ["1.2.3.4:1"]},
        "emulator": {"manager": r"D:\evil\MuMuManager.exe", "vmindex": 7},
        "cloud": {"base_url": "https://attacker.example", "token": "stolen"},
        # logging 现在**允许**下发（运维策略：保留天数/自动清理），
        # 但只放行白名单内的键 —— 段里的其它键必须被切掉。
        "logging": {"keep_days": 7, "shots_keep_days": 3, "shots_max_mb": 1500,
                    "adb_path": r"D:\evil\adb.exe"},
    }
    f = filter_payload(payload)
    check("只留下白名单段（tasks + logging）",
          set(f.keys()) == {"tasks", "logging"}, str(sorted(f.keys())))
    check("device 段被拦掉", "device" not in f)
    check("emulator 段被拦掉", "emulator" not in f)
    check("cloud 段被拦掉（后端不能给自己下发凭据）", "cloud" not in f)
    check("logging 段可下发（保留策略属运维，服务端可统一调配）", "logging" in f)
    check("logging 白名单内的键传过来了", f["logging"]["keep_days"] == 7)
    check("logging 段里白名单外的键被切掉", "adb_path" not in f["logging"])
    check("白名单内的值原样保留", f["tasks"]["yanwu"] is False)

    # 深合并：本地多出来的键（注释、本机段）不能被抹掉
    local = {
        "tasks": {"yanwu": True, "gongpin": True},
        "recruit": {"half_price": True, "_说明": "本地注释"},
        "device": {"adb": r"C:\Program Files\Netease\MuMu\nx_main\adb.exe"},
        "emulator": {"vmindex": 0},
    }
    deep_merge(local, {"recruit": {"half_price_max_hufu": 50}})
    check("深合并只覆盖带了的键", local["recruit"]["half_price_max_hufu"] == 50)
    check("深合并保留本地注释键", local["recruit"]["_说明"] == "本地注释")
    check("深合并保留同段其它键", local["recruit"]["half_price"] is True)

    d = diff_summary({"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"c": 9}})
    check("diff_summary 只列出真的变了的键", d == ["b.c: 2 → 9"], str(d))
    d2 = diff_summary({"x": [1, 2]}, {"x": [1, 2, 3]}, "sec")
    check("diff_summary 能识别列表变化", d2 == ["sec.x: [1, 2] → [1, 2, 3]"], str(d2))
    check("diff_summary 没变化时返回空", diff_summary({"a": 1}, {"a": 1}) == [])

    # apply_remote：正常下发
    class FakeClient:
        def __init__(self, resp, ok=True):
            self.resp, self.ok = resp, ok

        def fetch_config(self, version=0):
            return self.ok, self.resp

    tmp = tempfile.mkdtemp(prefix="stzb_rc_")
    try:
        state = os.path.join(tmp, "remote_config.json")
        cfg = {
            "tasks": {"yanwu": True, "gongpin": True},
            "recruit": {"half_price": True, "_说明": "本地"},
            "device": {"adb": r"C:\local\adb.exe"},
            "emulator": {"vmindex": 0},
            "cloud": {"base_url": "https://mine.example", "token": "mine"},
        }
        before_device = _json.dumps(cfg["device"], sort_keys=True)
        before_cloud = _json.dumps(cfg["cloud"], sort_keys=True)

        resp = {"version": 3, "note": "改演武", "payload": {
            "tasks": {"yanwu": False},
            "device": {"adb": r"D:\evil\adb.exe"},
            "emulator": {"vmindex": 7},
            "cloud": {"base_url": "https://attacker.example", "token": "stolen"},
        }}
        logs = []
        res = apply_remote(cfg, FakeClient(resp), state, logger=logs.append)
        check("apply_remote 成功且标记 changed", res["ok"] and res["changed"])
        check("远端改了 tasks.yanwu", cfg["tasks"]["yanwu"] is False)
        check("本地多出来的任务没被删", cfg["tasks"]["gongpin"] is True)
        check("本地注释键还在", cfg["recruit"].get("_说明") == "本地")
        check("★ device 段没被远端改动", _json.dumps(cfg["device"], sort_keys=True) == before_device)
        check("★ cloud 段没被远端改动", _json.dumps(cfg["cloud"], sort_keys=True) == before_cloud)
        check("emulator.vmindex 没被改成 7", cfg["emulator"]["vmindex"] == 0)
        check("版本号落盘", _json.load(open(state, encoding="utf-8"))["version"] == 3)

        # 版本没变 -> 不再覆盖
        cfg["tasks"]["yanwu"] = True
        res = apply_remote(cfg, FakeClient(resp), state, logger=logs.append)
        check("版本号相同时不重复覆盖", cfg["tasks"]["yanwu"] is True and not res["changed"])

        # 拉不到 -> 本地必须原样不动
        cfg2 = {"tasks": {"yanwu": True}, "device": {"adb": "C:/keep"}}
        res = apply_remote(cfg2, FakeClient(None, ok=False), os.path.join(tmp, "s2.json"),
                           logger=logs.append)
        check("拉取失败时不动本地配置", res["ok"] is False and cfg2["tasks"]["yanwu"] is True)

        def boom(version=0):
            raise OSError("网络断了")
        res = apply_remote(cfg2, type("C", (), {"fetch_config": staticmethod(boom)})(),
                           os.path.join(tmp, "s3.json"), logger=logs.append)
        check("拉取抛异常时不崩也不动本地", res["ok"] is False and res["error"] != "")

        # 领取待执行任务
        class JobClient:
            def __init__(self, jobs, take_ok=True):
                self.jobs, self.take_ok = jobs, take_ok
                self.took = []

            def list_jobs(self):
                return True, {"jobs": self.jobs}

            def take_job(self, jid):
                self.took.append(jid)
                return self.take_ok

        jc = JobClient([{"id": 7, "slot": "12:00", "only": "yanwu", "dry_run": True,
                         "note": "验证演武"}])
        job = pick_job(jc, logger=logs.append)
        check("能领到待执行任务", job and job["id"] == 7 and jc.took == [7])
        check("空队列返回 None", pick_job(JobClient([]), logger=logs.append) is None)
        check("领取失败（已被别人领走）返回 None",
              pick_job(JobClient([{"id": 8}], take_ok=False), logger=logs.append) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cloud_upload():
    """上传模块：multipart 编码、本地报告包装、TLS、失败可控。"""
    import json as _json
    import shutil
    import tempfile

    import numpy as np

    from stzb.cloud import (CloudClient, CloudError, LocalReportStub,
                            encode_multipart, hostname, load_last_report,
                            shrink_to_jpeg, _ssl_context)
    from stzb.core import write_png

    print("\n[24] 后台上传（multipart / 本地报告 / TLS / 不抛异常）")

    body, ctype = encode_multipart(
        {"task_key": "yanwu", "index": "2"},
        [("file", "yw_01.jpg", b"\xff\xd8\xff\xe0FAKEJPEG", "image/jpeg")])
    check("multipart 的 Content-Type 带 boundary", ctype.startswith("multipart/form-data; boundary="))
    b = body.decode("latin-1")
    check("含表单字段 task_key", 'name="task_key"' in b and "yanwu" in b)
    check("含文件字段与文件名", 'name="file"; filename="yw_01.jpg"' in b)
    check("含文件 Content-Type", "Content-Type: image/jpeg" in b)
    check("以结束边界收尾", b.rstrip().endswith("--"))
    check("图片字节原样在 body 里", b"FAKEJPEG" in body)

    # 令牌/地址缺失要明确报错，不能带着空值去请求
    try:
        CloudClient(base_url="", token="x")
        check("空 base_url 应报错", False)
    except CloudError:
        check("空 base_url 报 CloudError", True)
    try:
        CloudClient(base_url="https://a.example", token="")
        check("空 token 应报错", False)
    except CloudError:
        check("空 token 报 CloudError", True)

    # TLS：必须有像样的根证书库，且不做降级校验
    ctx = _ssl_context()
    check("TLS 根证书数量正常（%d 个）" % len(ctx.get_ca_certs()),
          len(ctx.get_ca_certs()) > 50)
    check("证书校验是打开的（不降级）", ctx.verify_mode.name == "CERT_REQUIRED")
    check("hostname 非空", bool(hostname()))

    # 本地报告包装
    tmp = tempfile.mkdtemp(prefix="stzb_cloud_")
    try:
        shots = os.path.join(tmp, "shots")
        os.makedirs(shots, exist_ok=True)
        real = os.path.join(shots, "sj_01.png")
        write_png(real, np.full((60, 90, 3), 180, dtype=np.uint8))
        data = {
            "started_at": "2026-09-19T02:28:09",
            "slot": "00:00",
            "counts": {"ok": 1, "fail": 0, "skip": 0},
            "all_ok": True,
            "tasks": [
                {"key": "shijing", "name": "内政市井", "status": "ok",
                 "shots": [real, os.path.join(shots, "不存在.png")]},
            ],
        }
        stub = LocalReportStub(data)
        check("stub 解析 started_at", stub.started_at.year == 2026 and stub.started_at.hour == 2)
        check("stub 解析 slot", stub.slot == "00:00")
        check("stub 任务数为 1", len(stub.entries) == 1)
        check("stub 只保留真实存在的截图（不存在的丢掉）",
              stub.entries[0].shots == [real], str(stub.entries[0].shots))
        check("stub.to_dict 原样返回", stub.to_dict()["slot"] == "00:00")

        rdir = os.path.join(tmp, "reports")
        os.makedirs(rdir, exist_ok=True)
        with open(os.path.join(rdir, "latest.json"), "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
        html = os.path.join(rdir, "latest.html")
        with open(html, "w", encoding="utf-8") as f:
            f.write("<html>report</html>")
        got = load_last_report(rdir)
        check("load_last_report 读到最新那份", got is not None and got[1]["html"] == html)
        check("目录里没有 latest.json 时返回 None",
              load_last_report(os.path.join(tmp, "empty")) is None)

        # 截图瘦身：必须变小，且是合法 JPEG
        big = os.path.join(shots, "big.png")
        write_png(big, np.random.randint(0, 255, (900, 1600, 3), dtype=np.uint8))
        jpg = shrink_to_jpeg(big, width=1000, quality=76)
        check("缩图后是 JPEG 头", jpg is not None and jpg[:2] == b"\xff\xd8")
        check("缩图后明显变小（%d → %d 字节）" % (os.path.getsize(big), len(jpg or b"")),
              jpg is not None and len(jpg) < os.path.getsize(big))
        check("读不到的文件返回 None", shrink_to_jpeg(os.path.join(shots, "无.png")) is None)

        # 网络失败必须返回结果而不是抛异常（上传是尽力而为）
        c = CloudClient(base_url="http://127.0.0.1:9", token="t", retries=1, timeout=2,
                        logger=lambda m: None)
        ok, rid, err = c.create_run({"slot": "00:00"})
        check("连不上时 create_run 返回 False 而不抛异常", ok is False and rid is None and err)
        ok, err = c.upload_report(1, os.path.join(shots, "不存在.html"))
        check("报告文件不存在时返回可读错误", ok is False and "读不到" in err)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_mode():
    """后端绑定判定。2026-09-20 起独立模式已按用户要求移除，只剩「绑上了 / 还没绑」。

    要守住的两条：
      1. **没绑后端也必须能照常跑完任务**，本机报告完整，不许卡住、不许报错退出；
      2. 只要 base_url + token 齐了就是托管，**不再看 cloud.enabled**（该字段已废弃）。
    """
    import run_daily as rd

    print("\n[25] 后端绑定（托管 vs 未绑定）")

    logs = []
    lg = logs.append

    def cfg_of(cloud):
        return {"cloud": cloud}

    ok_pair = {"base_url": "https://x", "token": "t"}

    # 1) 配全了就是托管 —— 这是唯一一种「绑上了」的情形
    m = rd.resolve_mode(cfg_of(dict(ok_pair)), offline=False, log=lg)
    check("base_url + token 齐全 → 后端托管", m == rd.MODE_MANAGED, m)

    # 2) enabled=false 但地址令牌齐全 → 依然是托管（该字段已废弃，不再参与判定）
    m = rd.resolve_mode(cfg_of(dict(ok_pair, enabled=False)), offline=False, log=lg)
    check("enabled=false 不再影响判定（废弃字段）", m == rd.MODE_MANAGED, m)

    # 3) offline 参数已废弃：传 True 也不再能把托管压成未绑定
    m = rd.resolve_mode(cfg_of(dict(ok_pair)), offline=True, log=lg)
    check("offline 参数已废弃，传 True 仍判定为托管", m == rd.MODE_MANAGED, m)

    # 4) 缺任意一半 → 未绑定，且日志要**明确告警**
    for bad, why in (({}, "cloud 段为空"),
                     ({"token": "t"}, "缺 base_url"),
                     ({"base_url": "https://x"}, "缺 token"),
                     ({"base_url": "   ", "token": "t"}, "base_url 只有空白"),
                     ({"base_url": "https://x", "token": ""}, "token 是空串")):
        logs.clear()
        m = rd.resolve_mode(cfg_of(dict(bad)), offline=False, log=lg)
        check("未绑定（%s）→ MODE_UNBOUND 并告警" % why,
              m == rd.MODE_UNBOUND and any("还没绑定后端" in x for x in logs), m)

    # 5) 说明文字要能一眼分辨绑没绑（用户看日志/控制台就能判断）
    s = rd.describe_mode(cfg_of({}), rd.MODE_UNBOUND)
    check("未绑定的说明写明「只留在本机」", "只留在本机" in s, s)
    d = rd.describe_mode(cfg_of({"base_url": "https://abc.example"}), rd.MODE_MANAGED)
    check("托管模式的说明里带上了后端地址", "https://abc.example" in d, d)

    # 6) 未绑定时上传必须直接返回，一个字节都不发
    calls = []

    class BoomClient:
        def __getattr__(self, name):
            def _f(*a, **kw):
                calls.append(name)
                raise AssertionError("未绑定后端不该碰任何远端！调用了 %s" % name)
            return _f

    rc = rd._do_upload({"cloud": {}}, None, {"html": "x.html"},
                       None, None, lg, disabled=True, reason="未绑定后端")
    check("未绑定时 _do_upload 返回 None 且不做任何请求", rc is None and not calls)
    check("未绑定时会打印报告留在本机的位置",
          any("报告只留在本机" in x for x in logs), str(logs[-1:]))

    # client=None（托管但初始化失败）同样安全
    rc2 = rd._do_upload({"cloud": dict(ok_pair)}, None, {"html": "x.html"},
                        None, None, lg, disabled=False)
    check("client 为 None 时也不崩、不请求", rc2 is None and not calls)

    # 7) 未绑定时 --upload-last 要明确拒绝，并指引怎么绑，而不是偷偷去连
    import io as _io
    import contextlib
    import subprocess
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rd.cmd_upload_last({"cloud": {}})
    check("未绑定时 --upload-last 被拒绝并给出绑定指引",
          code == 1 and "还没绑定后端" in buf.getvalue(), buf.getvalue()[:120])

    # 8) 命令行里 --offline/--standalone 必须**已经不存在**
    out = subprocess.run([sys.executable, os.path.join(ROOT, "run_daily.py"), "--help"],
                         capture_output=True, text=True, errors="ignore")
    helptxt = out.stdout or ""
    check("--help 里已不再出现 --offline",
          "--offline" not in helptxt and "--standalone" not in helptxt)

    # 9) 源码里不许再留 MODE_STANDALONE（防止漏改回退）
    with open(os.path.join(ROOT, "run_daily.py"), encoding="utf-8") as f:
        src = f.read()
    check("run_daily.py 里已无 MODE_STANDALONE", "MODE_STANDALONE" not in src)

    # 10) 出厂 config.json 是「未绑定」—— 拷到别的机器不配任何东西也能直接跑
    #
    # ★ 为什么读 config.example.json 而不是 config.json（2026-09-21 修）：
    #   这条断言测的是「**出厂**配置」这个事实，而本机 config.json 早就绑上了
    #   自己的后端（cloud.base_url/token 都填齐了）—— 拿它去断言「未绑定」
    #   必然失败，和在别人机器上跑的结果还不一样，是个环境相关的假失败。
    #   出厂形态的权威来源是 config.example.json（随仓库发布的模板）。
    from stzb.config import load as _load
    ex_path = os.path.join(ROOT, "config.example.json")
    check("config.example.json 存在（出厂模板）", os.path.exists(ex_path))
    m = rd.resolve_mode(_load(ex_path), offline=False, log=lg)
    check("★ 出厂配置判定为未绑定（开箱即可照常跑任务）", m == rd.MODE_UNBOUND, m)

    # 10b) 本机 config.json 若已绑定，也必须是「填齐才托管」那种绑定（不是靠 enabled 开关）
    cfg = _load()
    cc = cfg.get("cloud") or {}
    if str(cc.get("base_url") or "").strip() and str(cc.get("token") or "").strip():
        check("本机 config.json 已绑定后端 → resolve_mode 报托管",
              rd.resolve_mode(cfg, offline=False, log=lg) == rd.MODE_MANAGED)
    else:
        check("本机 config.json 未绑定 → resolve_mode 报未绑定",
              rd.resolve_mode(cfg, offline=False, log=lg) == rd.MODE_UNBOUND)

    # 11) 本机必备段在默认值里都有兜底（config.json 丢了也能跑）
    for sec in ("device", "emulator", "cloud", "logging", "safety", "tasks"):
        check("DEFAULTS 里有 %s 段兜底" % sec, sec in cfg)

    # 12) ★ 真命令行走一遍：配了假后端地址，--status 也必须**快速返回**，
    #     不许卡在连不上的后端上（这是「后端挂了不能拖垮本机任务」的底线）。
    env = dict(os.environ)
    env.update({"STZB_CLOUD_BASE_URL": "http://127.0.0.1:9",
                "STZB_CLOUD_TOKEN": "t"})
    try:
        out = subprocess.run([sys.executable, os.path.join(ROOT, "run_daily.py"), "--status"],
                             capture_output=True, text=True, errors="ignore",
                             env=env, timeout=90)
        txt = (out.stdout or "") + (out.stderr or "")
        check("★ --status 报「后端托管」且不卡住", "后端托管" in txt, txt[-260:])
    except subprocess.TimeoutExpired:
        check("--status 应在 90 秒内返回（不该去连后端）", False, "超时")

    # 13) 环境变量注入必须是「不带 enabled 也能生效」的（enabled 已废弃）
    #
    # ★ 这里要造一个**未绑定**的配置来看 --status 怎么报 —— 但本机 config.json
    #   已经绑上自己的后端了（也在 .gitignore 里、不会提交），拿它跑这条必然失败。
    #   所以临时把 config.json 换成出厂模板（base_url/token 都空），看完再还原。
    #   不这么做的话，「本机已绑定」和「断言未绑定」是互相矛盾的，只能靠改本机配置
    #   来过测试 —— 那是把环境状态写进测试，不是测试代码。
    cfg_real = os.path.join(ROOT, "config.json")
    cfg_bak = cfg_real + ".selftest.bak"
    had_real = os.path.exists(cfg_real)
    try:
        if had_real:
            shutil.copy2(cfg_real, cfg_bak)
        shutil.copy2(os.path.join(ROOT, "config.example.json"), cfg_real)
        env2 = dict(os.environ)
        env2.pop("STZB_CLOUD_BASE_URL", None)
        env2.pop("STZB_CLOUD_TOKEN", None)
        out = subprocess.run([sys.executable, os.path.join(ROOT, "run_daily.py"), "--status"],
                             capture_output=True, text=True, errors="ignore",
                             env=env2, timeout=90)
        txt = (out.stdout or "") + (out.stderr or "")
        check("★ 没有环境变量 + 出厂配置时 --status 报「未绑定」",
              "未绑定" in txt, txt[-260:])
    finally:
        if had_real:
            shutil.move(cfg_bak, cfg_real)
        else:
            try:
                os.remove(cfg_real)
            except OSError:
                pass


def test_screenshot_retry():
    """adb 截屏会偶发失败，必须具备「重连 + 换手段」的重试，而不是直接让任务挂掉。"""
    import cv2
    import numpy as np

    from stzb.core import AdbError, Device

    print("\n[26] 截屏重试（adb exec-out 偶发失败时的三级兜底）")

    ok, buf = cv2.imencode(".png", np.full((30, 40, 3), 128, dtype=np.uint8))
    good = buf.tobytes()

    class FakeDev(Device):
        """把 adb 调用换掉，模拟各种失败组合。"""

        def __init__(self, plan):
            super().__init__(adb="no-such-adb")
            self.plan = list(plan)          # 每次 raw_bytes 的返回：bytes 或 Exception
            self.calls = []
            self.reconnected = 0
            self.shell_calls = []

        def raw_bytes(self, *args, timeout=None):
            self.calls.append(args)
            r = self.plan.pop(0) if self.plan else good
            if isinstance(r, Exception):
                raise r
            return r

        def raw(self, *args, check=True, timeout=None):
            self.shell_calls.append(args)
            return ""

        def adb_global(self, *args, timeout=None):
            if args and args[0] == "connect":
                self.reconnected += 1
            return ""

    # 1) 第一次就成功：不该多做任何事
    d = FakeDev([good])
    img = d.screenshot()
    check("一次成功时直接返回图像", img is not None and img.shape == (30, 40, 3))
    check("一次成功时不重连、不走兜底", d.reconnected == 0 and not d.shell_calls)

    # 2) 第一次失败、重连后成功 —— 这是最常见的真实情况
    d = FakeDev([AdbError("adb exec-out screencap -p 失败"), good])
    img = d.screenshot()
    check("第一次失败后重连再试成功", img is not None)
    check("确实重连了一次", d.reconnected == 1, str(d.reconnected))
    check("没走到最后的 shell 兜底", not d.shell_calls)

    # 3) 前两次都失败 → 退到「存到设备再 cat 回来」
    d = FakeDev([AdbError("失败1"), b"", good])
    img = d.screenshot()
    check("前两次失败后退到 shell 兜底并成功", img is not None)
    check("兜底走的是 shell screencap", bool(d.shell_calls) and d.shell_calls[0][0] == "shell")
    check("兜底里用 cat 把文件取回来",
          any("cat" in c for c in d.calls), str(d.calls))

    # 4) 三次全失败 → 抛 AdbError，且错误信息说清是连续失败
    d = FakeDev([AdbError("失败1"), AdbError("失败2"), AdbError("失败3")])
    try:
        d.screenshot()
        check("三次全失败应抛 AdbError", False)
    except AdbError as e:
        check("三次全失败抛 AdbError 且说明连续失败", "3 次失败" in str(e), str(e))

    # 5) 返回不可解码的数据也要被当成失败继续重试
    d = FakeDev([b"not-a-png", good])
    img = d.screenshot()
    check("拿到坏数据会继续重试而不是抛错", img is not None)

    # 6) 解码失败但数据非空时，不能把空数据当成功
    d = FakeDev([b"", b"", b""])
    try:
        d.screenshot()
        check("全是空数据应抛 AdbError", False)
    except AdbError as e:
        check("空数据也走完三次并报错", "3 次失败" in str(e))


def test_config_tool():
    """配置工具的 schema 覆盖率与读写安全性。"""
    import importlib.util
    import json as _json
    import shutil
    import tempfile

    from stzb import config as cfgmod

    spec = importlib.util.spec_from_file_location(
        "stzb_configtool", os.path.join(ROOT, "tools", "config.py"))
    tool = importlib.util.module_from_spec(spec)
    # 必须先注册进 sys.modules：@dataclass 会去 sys.modules[cls.__module__] 里查类型提示，
    # 不注册就报 AttributeError: 'NoneType' object has no attribute '__dict__'。
    sys.modules[spec.name] = tool
    spec.loader.exec_module(tool)

    print("\n[27] 配置工具（schema 覆盖 / 校验 / 读写不破坏文件）")

    # ---- 1) schema 必须覆盖 DEFAULTS 里的每一项（用户要求「能配所有内容」）
    missing = []
    for sec, val in cfgmod.DEFAULTS.items():
        if not isinstance(val, dict):
            continue
        for k in val:
            if k.startswith("_"):
                continue
            p = "%s.%s" % (sec, k)
            if p not in tool.ALL_FIELDS:
                missing.append(p)
    check("schema 覆盖 DEFAULTS 的每一个可配置项", not missing,
          "漏了：%s" % ", ".join(missing))

    extra = [p for p in tool.ALL_FIELDS
             if p.split(".")[0] not in cfgmod.DEFAULTS]
    check("schema 里没有指向不存在段的项", not extra, str(extra))

    check("每一项都能取到内置默认值",
          all(tool.ALL_FIELDS[p].default is not None for p in tool.ALL_FIELDS),
          str([p for p in tool.ALL_FIELDS if tool.ALL_FIELDS[p].default is None]))

    # ---- 2) 校验：错的要拦住，对的要放行
    f_int = tool.ALL_FIELDS["shuishou.max_times"]
    check("整数项拒绝非数字", tool.coerce(f_int, "abc")[0] is False)
    check("整数项拒绝越界", tool.coerce(f_int, "99")[0] is False)
    check("整数项接受合法值", tool.coerce(f_int, "3")[:2] == (True, 3))

    f_choice = tool.ALL_FIELDS["emulator.shutdown_after"]
    check("选项项拒绝非法值", tool.coerce(f_choice, "maybe")[0] is False)
    check("选项项接受 auto/always/never",
          all(tool.coerce(f_choice, c)[1] == c for c in ("auto", "always", "never")))

    f_bool = tool.ALL_FIELDS["cloud.pull_jobs"]
    check("布尔项认 true/false/是/否/1/0",
          [tool.coerce(f_bool, s)[1] for s in ("true", "否", "1", "off")]
          == [True, False, True, False])
    check("★ cloud.enabled 已从配置项里移除（独立模式废弃）",
          "cloud.enabled" not in tool.ALL_FIELDS)

    f_url = tool.ALL_FIELDS["cloud.base_url"]
    check("URL 项拒绝没有协议的写法", tool.coerce(f_url, "stzb.example.com")[0] is False)
    check("URL 项接受 https 并去掉结尾斜杠",
          tool.coerce(f_url, "https://a.example/")[:2] == (True, "https://a.example"))
    check("URL 项允许留空（= 不绑定）", tool.coerce(f_url, "")[:2] == (True, ""))

    f_path = tool.ALL_FIELDS["device.adb"]
    ok, _v, _e, soft = tool.coerce(f_path, r"D:\没有这个\adb.exe")
    check("路径项对不存在的路径返回 soft 失败（可 --force 放行）",
          ok is False and soft is True)

    f_list = tool.ALL_FIELDS["shijing.buy_materials"]
    check("清单项支持 、 和 , 分隔",
          tool.coerce(f_list, "甲、乙,丙")[1] == ["甲", "乙", "丙"])

    # ---- 3) 读写：不碰 _说明、不写没改过的键、unset 能回默认
    tmp = tempfile.mkdtemp(prefix="stzb_cfgtool_")
    real_cfg, real_bak, real_state = tool.CONFIG_PATH, tool.BACKUP_DIR, tool.REMOTE_STATE
    saved_cfgmod_path = cfgmod.CONFIG_PATH
    try:
        cfg_path = os.path.join(tmp, "config.json")
        tool.CONFIG_PATH = cfg_path
        tool.BACKUP_DIR = os.path.join(tmp, "backup")
        tool.REMOTE_STATE = os.path.join(tmp, "remote.json")
        cfgmod.CONFIG_PATH = cfg_path
        os.makedirs(tool.BACKUP_DIR, exist_ok=True)

        with open(cfg_path, "w", encoding="utf-8") as fo:
            _json.dump({"_说明": "顶层注释", "device": {"adb": "C:/real/adb.exe",
                                                    "_说明": "段内注释"},
                        "recruit": {"half_price": True}}, fo, ensure_ascii=False)

        check("is_explicit 认识文件里写了的键", tool.is_explicit("recruit.half_price") is True)
        check("is_explicit 认识没写的键", tool.is_explicit("shuishou.max_times") is False)

        tool.set_value("shuishou.max_times", 2, quiet=True)
        raw = _json.load(open(cfg_path, encoding="utf-8"))
        check("写入后值正确", raw["shuishou"]["max_times"] == 2)
        check("★ 保住了顶层 _说明", raw.get("_说明") == "顶层注释")
        check("★ 保住了段内 _说明", raw["device"].get("_说明") == "段内注释")
        check("没有把别的默认值一股脑写进文件", "tasks" not in raw, str(list(raw.keys())))
        check("写入自动产生了备份", len(os.listdir(tool.BACKUP_DIR)) >= 1)

        tool.unset_value("shuishou.max_times", quiet=True)
        raw = _json.load(open(cfg_path, encoding="utf-8"))
        check("unset 后键被删掉（回到内置默认值）", "max_times" not in raw.get("shuishou", {}))
        check("unset 后取到的仍是默认值", tool.get_effective("shuishou.max_times") == 3)

        # coerce 失败不该留下任何痕迹
        before = open(cfg_path, encoding="utf-8").read()
        f = tool.ALL_FIELDS["shuishou.max_times"]
        ok, _v, _e, _s = tool.coerce(f, "不是数字")
        check("★ 校验失败时没有写文件", ok is False
              and open(cfg_path, encoding="utf-8").read() == before)

        # 备份轮换：只保留最近 KEEP_BACKUPS 份
        for i in range(tool.KEEP_BACKUPS + 4):
            tool.set_value("logging.keep_days", 10 + i, quiet=True)
        n = len(os.listdir(tool.BACKUP_DIR))
        check("备份数量被限制在 %d 份以内（实际 %d）" % (tool.KEEP_BACKUPS, n),
              n <= tool.KEEP_BACKUPS)

        # 绑定失败不能改配置
        before = open(cfg_path, encoding="utf-8").read()
        tool.verify_backend("http://127.0.0.1:9", "t", timeout=2)
        check("★ verify_backend 失败时不碰配置文件",
              open(cfg_path, encoding="utf-8").read() == before)
    finally:
        tool.CONFIG_PATH, tool.BACKUP_DIR, tool.REMOTE_STATE = real_cfg, real_bak, real_state
        cfgmod.CONFIG_PATH = saved_cfgmod_path
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 4) 错误翻译要能照着做
    for raw_err, key in (("HTTP 401 {\"detail\":\"x\"}", "令牌"),
                         ("HTTP 404 ...", "地址"),
                         ("URLError: [WinError 10061]", "拒绝"),
                         ("certificate verify failed", "证书")):
        msg = tool.describe_error(raw_err)
        check("错误 %-28s 的提示里包含关键信息" % raw_err[:26], key in msg, msg)

    # ---- 5) 中文对齐：pad 按显示宽度补，不按字符数
    check("中文按两列计算宽度", tool.disp_width("中文ab") == 6)
    check("pad 后显示宽度等于目标（中文混排）",
          tool.disp_width(tool.pad("中文ab", 20)) == 20)
    check("pad 超宽会截断（带省略号，并补齐到目标宽度）",
          "…" in tool.pad("很长的中文内容" * 5, 10)
          and tool.disp_width(tool.pad("很长的中文内容" * 5, 10)) == 10)
    check("pad 对不足宽度的普通字符串左对齐补齐",
          tool.pad("abc", 6) == "abc   ")

    # ---- 6) 交互菜单：喂一串按键进去，验证真的改到了文件。
    #     这是用户双击 config_tool.bat 之后走的主路径，不能只测命令行。
    import contextlib
    import io as _io

    def run_menu(cfg_path, keys: str, backup_dir: str):
        tool.CONFIG_PATH = cfg_path
        tool.BACKUP_DIR = backup_dir
        tool.REMOTE_STATE = os.path.join(os.path.dirname(cfg_path), "remote.json")
        cfgmod.CONFIG_PATH = cfg_path
        os.makedirs(backup_dir, exist_ok=True)
        old_stdin, buf = sys.stdin, _io.StringIO()
        sys.stdin = _io.StringIO(keys)
        try:
            with contextlib.redirect_stdout(buf):
                tool.menu_main()
        finally:
            sys.stdin = old_stdin
        return buf.getvalue()

    tmp2 = tempfile.mkdtemp(prefix="stzb_cfgmenu_")
    real_cfg2, real_bak2, real_state2 = tool.CONFIG_PATH, tool.BACKUP_DIR, tool.REMOTE_STATE
    saved2 = cfgmod.CONFIG_PATH
    try:
        cfg2 = os.path.join(tmp2, "config.json")
        with open(cfg2, "w", encoding="utf-8") as fo:
            _json.dump({"_说明": "别弄丢我", "shuishou": {"max_times": 3}},
                       fo, ensure_ascii=False)

        # 菜单编号：1=后端托管，2..11=各配置组（cloud 组在 1 里），12=查看全部，13=备份
        # 6 = shuishou 组（device/tasks/recruit/shijing/shuishou/... → 第 5 个非 cloud 组）
        out = run_menu(cfg2, "6\n1\n2\n\n0\n0\n", os.path.join(tmp2, "bak"))
        raw = _json.load(open(cfg2, encoding="utf-8"))
        check("★ 交互菜单里改一项真的写进了文件", raw["shuishou"]["max_times"] == 2,
              str(raw))
        check("交互菜单也没弄丢 _说明", raw.get("_说明") == "别弄丢我")
        check("交互菜单打印了保存确认", "已保存" in out)

        # 走一遍「后端托管」子菜单（未绑定时选解绑，应提示无需解绑，然后返回）
        out = run_menu(cfg2, "1\n3\n\n0\n0\n", os.path.join(tmp2, "bak"))
        check("后端子菜单能进能出（选解绑时提示本来就没绑）",
              "本来就没绑后端" in out, out[-200:])

        # 「查看全部配置」「磁盘占用与清理」「备份与恢复」三个菜单项都要可达。
        # 编号按「实际列出的段数」算，加菜单项时这里的数字要跟着变。
        out = run_menu(cfg2, "12\n\n13\n0\n14\n1\n\n0\n0\n", os.path.join(tmp2, "bak"))
        check("菜单项「查看全部配置」可达", "cloud.base_url" in out)
        check("菜单项「磁盘占用与清理」可达", "磁盘占用与清理" in out and "截图" in out)
        check("菜单项「备份与恢复」可达并真的备份了",
              "已备份到" in out and len(os.listdir(os.path.join(tmp2, "bak"))) >= 1)
        # 编号不能撞：截取**一次**菜单渲染（从「本机配置工具」到「0) 退出」）再数编号，
        # 否则多轮菜单的输出会叠加进来，怎么数都是重复的。
        import re as _re
        blocks = _re.findall(r"本机配置工具(.*?0\) 退出)", out, _re.S)
        nums = _re.findall(r"^\s+(\d+)\) ", blocks[0], _re.M) if blocks else []
        dup = {x for x in nums if nums.count(x) > 1}
        check("菜单编号无重复（cloud 段跳过时容易撞号）",
              bool(nums) and not dup,
              "共 %d 个编号: %s%s" % (len(nums), sorted(nums, key=int),
                                    ("  重复: %s" % sorted(dup)) if dup else ""))
        # 编号含 0（退出）在内应当是「0..N 连续无缺口」
        got = sorted(int(x) for x in nums)
        check("菜单编号连续无缺口（0..%d）" % (len(got) - 1),
              got == list(range(len(got))), str(got))

        # 输入 q 能安全退出（不该崩）
        out = run_menu(cfg2, "q\n", os.path.join(tmp2, "bak"))
        check("顶层输入 q 能安全退出", "配置已保存在" in out)

        # 输入越界编号不崩
        out = run_menu(cfg2, "99\n0\n", os.path.join(tmp2, "bak"))
        check("输入不存在的编号只是提示，不崩", "没有这个编号" in out)

        # ---- 6.5) ★ 隔离铁律：清理菜单绝不能作用于真实项目根 ----
        # 2026-09-19 血的教训：磁盘清理菜单原先用模块级 ROOT，测试把 CONFIG_PATH
        # 指到临时目录后仍去清真实 logs/，一次「立即清理」真删掉 471 张截图。
        # 现在清理路径全部从 project_root()（= CONFIG_PATH 所在目录）派生，
        # 这两条断言把「隔离」钉死，防止以后有人改回 ROOT。
        saved_cfg3 = tool.CONFIG_PATH
        try:
            tool.CONFIG_PATH = os.path.join(tmp2, "config.json")
            real_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            check("隔离：project_root() 跟随 CONFIG_PATH，不是真实项目根",
                  tool.project_root() == tmp2,
                  "得到 %s" % tool.project_root())
            check("隔离：清理目标目录不在真实项目根下",
                  not os.path.abspath(os.path.join(tool.project_root(), "logs"))
                  .startswith(os.path.abspath(real_root) + os.sep),
                  tool.project_root())
        finally:
            tool.CONFIG_PATH = saved_cfg3
    finally:
        tool.CONFIG_PATH, tool.BACKUP_DIR, tool.REMOTE_STATE = real_cfg2, real_bak2, real_state2
        cfgmod.CONFIG_PATH = saved2
        shutil.rmtree(tmp2, ignore_errors=True)


def test_bat_files():
    r"""批处理启动器的卫生检查。

    背景：config_tool.bat 里写过 `cd /d "%~dp0.."` —— `%~dp0` 本来就以反斜杠结尾，
    再接 `..` 会往上一级跳，从项目根目录跳到上级目录，python 就找不到
    tools\config.py 了。用户双击立刻报错：
        can't open file 'E:\Project\tools\config.py': No such file or directory
    这类 bug 只有真机（双击 bat）才暴露，Python 自检跑不到 —— 所以这里静态扫一遍
    项目根目录下所有 .bat，盯住这类模式。别删这条，它是「bat 也会写错」的教训。
    """
    import io as _io

    print("\n[28] 批处理启动器卫生（cd 目录 / Python 路径）")

    bats = sorted(f for f in os.listdir(ROOT)
                  if f.lower().endswith(".bat") and os.path.isfile(os.path.join(ROOT, f)))
    check("找到了 bat 启动器", len(bats) >= 3, str(bats))

    for name in bats:
        p = os.path.join(ROOT, name)
        s = _io.open(p, encoding="utf-8", errors="replace").read()
        tag = "「%s」" % name
        # 只看真正执行的代码行：rem 注释里提到危险的写法不应算数（否则误伤说明文字）
        code = "\n".join(ln for ln in s.splitlines()
                         if ln.strip() and not ln.strip().lower().startswith("rem"))
        # 关键：必须留在 bat 自己所在目录（项目根），绝不能 `%~dp0..` 往上跳
        check(tag + " 有 `cd /d \"%~dp0\"`", 'cd /d "%~dp0"' in code,
              "看看 bat 里的 cd 行")
        check(tag + " 没有危险的 `%~dp0..`（往上跳一级会找不到脚本）",
              "%~dp0.." not in code)
        # 克隆即用：优先用项目本地 venv，绝不能硬编码某个人机器上的绝对 Python 路径
        check(tag + " 优先用项目本地 venv", "venv\\Scripts\\python.exe" in s)
        check(tag + " 没硬编码 C:\\Users 下的绝对 Python 路径（别人能克隆直接跑）",
              not any("C:\\Users\\" in ln for ln in code.splitlines()))
        check(tag + " 是 ASCII（中文注释会乱码）",
              all(ord(c) < 128 for c in s), "bat 里出现非 ASCII 字符")


def test_config_autoresolve():
    """config.py 的路径自动探测：别人 clone 下来、MuMu 装在非默认位置也能跑。"""
    import json as _json
    import shutil
    import tempfile

    from stzb import config as cfgmod

    print("\n[29] 配置路径自动探测（克隆即用）")

    tmp = tempfile.mkdtemp(prefix="stzb_mumu_")
    old_cands = cfgmod._MUMU_ROOT_CANDIDATES
    try:
        # 造一个假的 MuMu 目录结构，当成唯一的候选（避免依赖真机是否装了 MuMu）
        fake_root = os.path.join(tmp, "Program Files", "Netease", "MuMu")
        os.makedirs(os.path.join(fake_root, "nx_main"), exist_ok=True)
        adb = os.path.join(fake_root, "nx_main", "adb.exe")
        mgr = os.path.join(fake_root, "nx_main", "MuMuManager.exe")
        open(adb, "w").close()
        open(mgr, "w").close()

        # 配置里显式写了错误路径 + 候选里只有 fake → 应自动探测到 fake
        cfgmod._MUMU_ROOT_CANDIDATES = [fake_root]
        c1 = os.path.join(tmp, "c1.json")
        _json.dump({"device": {"adb": r"D:\not\here\adb.exe"},
                    "emulator": {"manager": r"D:\not\here\MuMuManager.exe"}},
                   open(c1, "w", encoding="utf-8"))
        cfg = cfgmod.load(c1)
        check("adb 路径不存在时自动探测到候选", cfg.get("device.adb") == adb,
              cfg.get("device.adb"))
        check("manager 路径不存在时自动探测到候选", cfg.get("emulator.manager") == mgr,
              cfg.get("emulator.manager"))

        # 显式给了正确路径时，保持原值不动（别自作主张覆盖）
        c2 = os.path.join(tmp, "c2.json")
        _json.dump({"device": {"adb": adb}, "emulator": {"manager": mgr}},
                   open(c2, "w", encoding="utf-8"))
        cfg2 = cfgmod.load(c2)
        check("显式路径存在时保持原值",
              cfg2.get("device.adb") == adb and cfg2.get("emulator.manager") == mgr)

        # 候选全空 + 错误路径 → 探测不到就保持原值，别静默改成空/别的
        cfgmod._MUMU_ROOT_CANDIDATES = []
        c3 = os.path.join(tmp, "c3.json")
        _json.dump({"device": {"adb": r"D:\not\here\adb.exe"}},
                   open(c3, "w", encoding="utf-8"))
        cfg3 = cfgmod.load(c3)
        check("探测不到时保持原值（跑起来会有明确报错）",
              cfg3.get("device.adb") == r"D:\not\here\adb.exe")
    finally:
        cfgmod._MUMU_ROOT_CANDIDATES = old_cands
        shutil.rmtree(tmp, ignore_errors=True)


def test_title_page():
    """冷启动标题页识别（2026-09-19 真机踩到的 bug）。

    症状：游戏冷启动后停在标题页（山水画 + 底部金字「点击以开始游戏」），
    这一屏**全屏 OCR 读 0 行**，脚本认不出 → 一直走「认不出的界面」分支 →
    盲点右上角 ✕ 直到 300 秒启动超时，整轮任务全废。

    修法：模板匹配（templates/title_page.png），阈值 0.70。
    实测区分度：标题页 ≥0.85，非标题页（黑屏/网易开屏）≤0.42。
    """
    print("\n[N] 标题页识别（冷启动第一屏）")
    import os
    import numpy as np

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tpl_path = os.path.join(root, "templates", "title_page.png")
    check("模板文件存在", os.path.exists(tpl_path), tpl_path)
    if not os.path.exists(tpl_path):
        return

    # 用一张真机标题页截图当「正样本」，一张纯色图当「负样本」。
    shots = os.path.join(root, "logs", "shots")
    pos = None
    if os.path.isdir(shots):
        import glob
        cands = sorted(glob.glob(os.path.join(shots, "boot_*.png")))
        # 找一张能命中模板的（真机残留截图，存在就顺带验证）
        for c in cands:
            try:
                from stzb.core import read_png
                img = read_png(c)
                if img is None:
                    continue
                tpl = read_png(tpl_path)
                if tpl is not None and img.shape[0] >= tpl.shape[0]:
                    import cv2
                    r = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
                    if float(r.max()) >= 0.85:
                        pos = c
                        break
            except Exception:
                continue

    from stzb.core import Templates, read_png
    tpl = Templates(os.path.join(root, "templates"))

    # 负样本：纯色图不该命中（阈值 0.70 必须在噪声上不误报）
    blank = _blank()
    hit_blank = tpl.find(blank, "title_page", roi=(0, 860, 1920, 200), threshold=0.70)
    check("纯色图不误报为标题页", hit_blank is None,
          "命中=%s" % (hit_blank,))

    # 负样本：白底（网易开屏）也不该命中
    white = np.full((1080, 1920, 3), 255, dtype=np.uint8)
    hit_white = tpl.find(white, "title_page", roi=(0, 860, 1920, 200), threshold=0.70)
    check("白底开屏不误报为标题页", hit_white is None, "命中=%s" % (hit_white,))

    # 正样本：有真机截图就验证能命中
    if pos:
        img = read_png(pos)
        hit = tpl.find(img, "title_page", roi=(0, 860, 1920, 200), threshold=0.70)
        check("真机标题页截图能命中", hit is not None,
              "%s -> %s" % (os.path.basename(pos), hit))
        if hit:
            check("命中的 y 坐标落在标题文字带内（900~1045）",
                  900 <= hit[1] <= 1045, "y=%s" % hit[1])
    else:
        print("  [SKIP] 本机没有可用的真机标题页截图，跳过正样本验证")

    # 阈值余量：确认模板与自身完全匹配时分数接近 1.0
    selfimg = read_png(tpl_path)
    if selfimg is not None:
        h = tpl.find(selfimg, "title_page", threshold=0.70)
        check("模板对自身命中且分数 ≥0.99", h is not None and h[2] >= 0.99,
              "hit=%s" % (h,))


def test_home_recruit_missed():
    """「招募」按钮偶发漏读时，主城仍要被认出来。

    真机症状（2026-09-19 12:00 档）：市井任务报「打不开内政面板」，
    同一轮的演武/特性却都成功进去了 —— 因为那一帧右下角「招募」
    漏读了，is_home 判 False，open_neizheng() 以为人还在外面，
    反复退回主城重进，最后放弃。
    """
    print("\n[O] 主城判定：招募漏读时的兜底")
    from stzb.ui import Ui
    ui = Ui.__new__(Ui)
    ui.log = lambda m: None
    ui.dry_run = True
    ui.never_tap = ()
    ui.shots = []
    ui._tpl = None

    # 招募漏读，但有短「势力值」标签 + 主城导航词 → 必须判成主城
    missed = [itc("云魇丨奈子", 200, 60), itc("势力值21", 210, 120),
              itc("任务", 60, 105), itc("活动", 296, 175), itc("出征", 700, 900),
              itc("计略", 900, 900)]
    check("招募漏读但有短「势力值」→ 判为主城", ui.is_home(missed),
          [x.text for x in missed])

    # 长句里出现「势力值」不能当主城（活动面板的说明文字）
    longtext = [itc("世崛起，每日登录和提升势力值可获得势力积分，提升等级获得大量奖励",
                    900, 500)]
    check("长句里的「势力值」不算主城证据", not ui.is_home(longtext),
          [x.text for x in longtext])

    # _short_has 长度闸门
    check("_short_has 拒绝超长文本", not ui._short_has(longtext, "势力值"),
          "maxlen=10 应拦下这句长文本")
    check("_short_has 接受短标签", ui._short_has(missed, "势力值"), "势力值21")

    # 子面板特征词仍然优先排除（哪怕同时有 势力值）
    panel = [itc("势力值21", 210, 120), itc("宝物商队", 755, 143)]
    check("子面板（宝物商队）不被判成主城", not ui.is_home(panel),
          [x.text for x in panel])


def test_foreground_guard():
    """前台把关：用 dumpsys 的 mCurrentFocus 判断「游戏在不在前台」。

    为什么需要（2026-09-19 真机实测撞到）：
    模拟器起着、**游戏没起来**时，OCR 读到的是 MuMu 桌面/启动器，而 boot()
    会一路走「认不出的界面」分支**盲点右上角 ✕**，直到 300 秒超时 ——
    实测整整空转 7 分 48 秒，一个任务都没跑成。

    ★ 一并记住**实测得出的边界**（50 帧全量采样）：
      游戏内 mCurrentFocus **恒定**是 com.netease.stzb.netease/com.netease.stzb.Client，
      **不随游戏内界面切换而变** → 它**不能**用来判「在主城还是税收面板」。
      所以断言里同时守住「能判前台」和「不据此判界面」两条。
    """
    print("\n[P] 前台把关（dumpsys mCurrentFocus）")
    import stzb.core as core

    dev = core.Device.__new__(core.Device)      # 不跑 __init__，避免连设备
    PKG = "com.netease.stzb.netease"

    # —— 正样本：真机采集到的原始文本（一字不改，直接拿来当输入）
    REAL_GAME = ("mCurrentFocus=Window{598f027 u0 "
                 "com.netease.stzb.netease/com.netease.stzb.Client}")
    REAL_LAUNCHER = ("mCurrentFocus=Window{d54c7b3 u0 "
                     "com.netease.stzb.netease/com.netease.stzb.Launcher}")
    REAL_DESKTOP = ("mCurrentFocus=Window{8e31bf4 u0 "
                    "app.lawnchair/app.lawnchair.Launcher}")

    cases = [
        (REAL_GAME, PKG, "游戏内 → 解析出游戏包名"),
        (REAL_DESKTOP, "app.lawnchair", "MuMu 桌面 → 解析出桌面包名"),
        (REAL_LAUNCHER, PKG, "游戏启动器 → 仍是游戏包名"),
        ("mCurrentFocus=null", "", "null → 空串"),
        ("", "", "空输入 → 空串"),
        ("  mCurrentFocus=Window{f u0 com.android.systemui/com.android.systemui.panel}  ",
         "com.android.systemui", "带前后空格也能解析"),
    ]
    for text, want, name in cases:
        dev.foreground = lambda t=text: t
        got = dev.foreground_pkg()
        check(name, got == want, "得到 %r，期望 %r" % (got, want))

    # activity 解析
    dev.foreground = lambda: REAL_GAME
    check("能解析出前台 Activity",
          dev.foreground_activity() == "%s/com.netease.stzb.Client" % PKG,
          dev.foreground_activity())

    # game_foreground 三态
    for text, want, name in [
        (REAL_GAME, True, "在游戏里 → True"),
        (REAL_DESKTOP, False, "在桌面上 → False"),
        (REAL_LAUNCHER, True, "在游戏启动器里 → True（同包名）"),
        ("mCurrentFocus=null", True, "读不到 → True（放行，不误拦）"),
    ]:
        dev.foreground = lambda t=text: t
        check(name, dev.game_foreground(PKG) is want)

    # ★ 边界断言：前台信息在游戏内恒定，因此**不得**参与界面判定
    #   把 handler 的源码拿来查：界面判定函数里不许出现 foreground
    import inspect
    import stzb.ui as ui_mod
    src = inspect.getsource(ui_mod.Ui.is_home)
    check("is_home 不得依赖前台信息（它判不了界面）",
          "foreground" not in src)
    src2 = inspect.getsource(ui_mod.Ui.is_neizheng)
    check("is_neizheng 不得依赖前台信息",
          "foreground" not in src2)

    # 有 TTL 缓存，且读不到时不抛异常
    class _Dev:
        def __init__(self, raises=False):
            self.calls = 0
            self.raises = raises

        def game_foreground(self, pkg=None):
            self.calls += 1
            if self.raises:
                raise RuntimeError("adb 挂了")
            return True

        def foreground_pkg(self):
            return "com.netease.stzb.netease"

    d = _Dev()
    u = ui_mod.Ui.__new__(ui_mod.Ui)
    u.dev = d
    u.pkg = PKG
    u._fg_cache = (0.0, True)
    u.log = lambda *a, **k: None
    import time as _t
    u._fg_cache = (_t.time(), True)
    a = u.game_foreground()
    b = u.game_foreground()
    check("TTL 缓存生效（3 秒内不重复查 adb）", a and b and d.calls == 0,
          "实际调用 %d 次" % d.calls)

    u._fg_cache = (0.0, True)
    u.game_foreground(ttl=0.0)
    first = d.calls
    u.game_foreground(ttl=0.0)
    check("ttl=0 时每次都查", d.calls == first + 1, "%d → %d" % (first, d.calls))

    d2 = _Dev(raises=True)
    u2 = ui_mod.Ui.__new__(ui_mod.Ui)
    u2.dev = d2
    u2.pkg = PKG
    u2._fg_cache = (0.0, True)
    u2.log = lambda *a, **k: None
    check("查前台抛异常时放行（不把正常流程拦死）",
          u2.game_foreground(ttl=0.0) is True)


def test_cleanup():
    """日志/截图清理：只删自己的、当天不动、按体积兜底、不碰目录与符号链接。

    这一组是**保护性断言** —— 清理逻辑写错就是删用户文件，比别的 bug 严重得多，
    所以每条「不该删」都要有断言钉住。
    """
    import tempfile
    import time as _t
    from stzb import cleanup as cl

    print("\n[26] 日志/截图清理（保留天数 + 体积上限 + 保护规则）")

    root = tempfile.mkdtemp(prefix="stzb_clt_")
    logs = os.path.join(root, "logs")
    shots = os.path.join(logs, "shots")
    reports = os.path.join(logs, "reports")
    diag = os.path.join(logs, "diag")
    for d in (shots, reports, diag):
        os.makedirs(d, exist_ok=True)

    def mk(folder, name, days_old, size=100):
        p = os.path.join(folder, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        t = _t.time() - days_old * 86400
        os.utime(p, (t, t))
        return p

    # ---- ① 按天数清理：老的走、新的留 ----
    mk(shots, "old_001.png", 30)
    mk(shots, "old_002.png", 20)
    mk(shots, "new_003.png", 1)
    mk(shots, "today_004.png", 0)
    mk(shots, "_probe_keep.png", 99)          # 下划线开头 = 侦察对比图
    mk(reports, "run_old.html", 30)
    mk(reports, "run_new.html", 1)
    mk(reports, "latest.html", 99)            # 快捷入口
    mk(reports, "latest.json", 99)
    mk(logs, "run_2020-01-01.log", 30)
    mk(logs, "console.log", 99)               # 实时的控制台日志
    mk(diag, "band_a.png", 30)
    mk(diag, "eval_home.py", 30)              # 侦察脚本
    os.makedirs(os.path.join(diag, "sub"), exist_ok=True)
    mk(os.path.join(diag, "sub"), "deep.png", 30)

    res = cl.cleanup_by_days(root, keep_days=14, shots_max_mb=0,
                             logger=lambda m: None)

    check("过期截图被删", not os.path.exists(os.path.join(shots, "old_001.png")))
    check("较老的截图也被删", not os.path.exists(os.path.join(shots, "old_002.png")))
    check("未过期的截图保留", os.path.exists(os.path.join(shots, "new_003.png")))
    check("当天截图保留", os.path.exists(os.path.join(shots, "today_004.png")))
    check("下划线开头的侦察图不删",
          os.path.exists(os.path.join(shots, "_probe_keep.png")))
    check("过期报告被删", not os.path.exists(os.path.join(reports, "run_old.html")))
    check("未过期报告保留", os.path.exists(os.path.join(reports, "run_new.html")))
    check("latest.html 永不删", os.path.exists(os.path.join(reports, "latest.html")))
    check("latest.json 永不删", os.path.exists(os.path.join(reports, "latest.json")))
    check("过期文本日志被删",
          not os.path.exists(os.path.join(logs, "run_2020-01-01.log")))
    check("console.log 永不删", os.path.exists(os.path.join(logs, "console.log")))
    check("diag 里的过期图片被删", not os.path.exists(os.path.join(diag, "band_a.png")))
    check("diag 里的 .py 绝不动", os.path.exists(os.path.join(diag, "eval_home.py")))
    check("diag 子目录绝不动", os.path.isdir(os.path.join(diag, "sub")))
    check("子目录里的文件不递归删",
          os.path.exists(os.path.join(diag, "sub", "deep.png")))
    check("删掉的都是自己的文件（新文件一个没少）",
          len([n for n in os.listdir(shots) if n.endswith(".png")]) == 3)
    check("释放字节数 > 0", res["freed"] > 0)

    # ---- ② 体积上限：从最老开始删，当天豁免 ----
    root2 = tempfile.mkdtemp(prefix="stzb_clt2_")
    shots2 = os.path.join(root2, "logs", "shots")
    os.makedirs(shots2, exist_ok=True)
    for name, days in (("a_old.png", 3), ("b_old.png", 2), ("c_old.png", 1),
                       ("today.png", 0)):
        p = os.path.join(shots2, name)
        with open(p, "wb") as f:
            f.write(b"x" * 1048576)           # 每个 1MB
        t = _t.time() - days * 86400
        os.utime(p, (t, t))
    cl.cleanup_by_days(root2, keep_days=0, shots_keep_days=0,
                       shots_max_mb=2, diag=False, logger=lambda m: None)
    left = set(os.listdir(shots2))
    check("体积超限时从最老的开始删", "a_old.png" not in left, str(sorted(left)))
    check("体积降到上限以内", cl.usage(shots2, cl.IMG_EXT)["bytes"] <= 2 * 1048576)
    check("★ 当天文件在体积清理中也被保住", "today.png" in left)

    # ---- ③ keep_days=0 且不限体积 = 什么都不删 ----
    root3 = tempfile.mkdtemp(prefix="stzb_clt3_")
    shots3 = os.path.join(root3, "logs", "shots")
    os.makedirs(shots3, exist_ok=True)
    mk(shots3, "very_old.png", 300)
    cl.cleanup_by_days(root3, keep_days=0, shots_keep_days=0,
                       shots_max_mb=0, diag=False, logger=lambda m: None)
    check("keep_days=0 且不限体积 → 再老也不删",
          os.path.exists(os.path.join(shots3, "very_old.png")))

    # ---- ④ 幂等：连跑两次结果一致（不该越删越多） ----
    r1 = cl.cleanup_by_days(root, keep_days=14, shots_max_mb=0,
                            logger=lambda m: None)
    r2 = cl.cleanup_by_days(root, keep_days=14, shots_max_mb=0,
                            logger=lambda m: None)
    check("重复清理是幂等的（第二次删 0 个）",
          r2["deleted"] == 0, "第一次 %d / 第二次 %d" % (r1["deleted"], r2["deleted"]))

    # ---- ⑤ 目录不存在也不崩 ----
    try:
        cl.cleanup_by_days(os.path.join(root, "nonexistent_dir"),
                           keep_days=14, logger=lambda m: None)
        check("目录不存在时不抛异常", True)
    except Exception as e:
        check("目录不存在时不抛异常", False, repr(e))

    # ---- ⑥ 配置解析：-1 = 跟随，0 = 不清理（语义不能混） ----
    class _Cfg(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)
    c1 = _Cfg({"logging.keep_days": 7, "logging.shots_keep_days": -1,
               "logging.shots_max_mb": 0})
    p1 = cl.parse_cfg(c1, root)
    check("shots_keep_days=-1 解析为「跟随 keep_days」", p1["shots_keep_days"] is None)
    check("keep_days 原样读出", p1["keep_days"] == 7)
    check("shots_max_mb=0 表示不限", p1["shots_max_mb"] == 0)
    c2 = _Cfg({"logging.shots_keep_days": 0})
    check("shots_keep_days=0 表示「不按天清」而非「跟随」（0 与 -1 语义不同）",
          cl.parse_cfg(c2, root)["shots_keep_days"] == 0)
    check("配置缺项时用默认值不崩",
          cl.parse_cfg(_Cfg({}), root)["keep_days"] == 14)

    # ---- ⑦ 收尾清理的契约：任何异常都不许往外抛 ----
    # 2026-09-20 实测事故：清理抛异常后，整个收尾被跳过 ——
    # «清理后占用» 那行没打、last_run.json 没更新（还停在昨天）。
    # 后果是下次调度以为今天没跑，**重复跑一整轮、重复领奖**，
    # 这比直接失败更难发现。所以这条契约必须钉死。
    import run_daily as rd

    on = _Cfg({"logging.cleanup_enabled": True})   # 必须开着，否则函数提前 return

    def _raise_systemexit(*a, **kw):
        raise SystemExit(1)        # ★ 故意用**非 Exception**，才是踩过的那条路径

    orig_cbd = rd.cleanup_by_days
    msgs = []
    try:
        rd.cleanup_by_days = _raise_systemexit
        escaped = None
        try:
            rd._do_cleanup(on, msgs.append)
        except BaseException as e:
            escaped = e
        check("收尾清理吞掉非 Exception 异常（SystemExit），不往外抛",
              escaped is None, repr(escaped))
        check("异常被记进日志，不是静默吞掉",
              any("清理过程异常" in m for m in msgs), repr(msgs[-1:]))

        def _raise_kbd(*a, **kw):
            raise KeyboardInterrupt()

        rd.cleanup_by_days = _raise_kbd
        km = []
        escaped = None
        try:
            rd._do_cleanup(on, km.append)
        except BaseException as e:
            escaped = e
        check("Ctrl-C 中断清理时不外抛（清理立刻停，但收尾继续走完）",
              escaped is None, repr(escaped))
        check("Ctrl-C 有专门提示，不会被当成普通异常",
              any("Ctrl-C" in m for m in km), repr(km[-1:]))
    finally:
        rd.cleanup_by_days = orig_cbd


def test_panel_ocr_fallback():
    print("\n[30] 面板判定：深色面板整屏漏读时，必须补一轮放大 OCR")
    # 2026-09-20 00:29 实测失败：税收面板是深色插画底 + 小字，
    # 1920x1080 原图 OCR 整屏**只读到 1 行**（真值 12 行，连「征收」「等待中」
    # 都读不出）→ _in_panel("税收") 判 False → open_sub 以为「当前不在内政界面」
    # → 退回主城重进 → 反复空转 → 最后报「进不去「税收」」，整轮任务失败。
    # 同一帧放大 2 倍能读到 12 行。所以判定路径必须带放大兜底。
    import stzb.ui as _ui

    # ---- ① 「可强征N/3」是内政界面上的入口状态标签，不是「已在税收面板」 ----
    # 曾经把「强征」当税收面板的锚点，于是内政界面 OCR 只读到 1 个锚点
    # （_is_neizheng_screen 认不出）时会被误判成「已经在税收面板里了」，
    # 任务就在内政界面上找征收按钮，静默错位。
    ui = FakeUi()
    thin_nz = [itc("市井", 1045, 700), itc("可强征0/3", 1743, 769)]
    check("内政界面（锚点不足）+「可强征0/3」→ 不算已在税收面板",
          ui._in_panel("税收", thin_nz) is False)

    # 真税收面板的三个格子标题必须照样认得出
    real_ss = [itc("征收", 427, 784), itc("征收", 948, 784), itc("征收", 1467, 784),
               itc("20/征收", 1511, 877), itc("等待中···", 1467, 900)]
    check("真税收面板仍判「已在里面」", ui._in_panel("税收", real_ss) is True)

    # 最坏状态：三格全是付费「20/强征」——三格的「征收」标题还在，必须照样认得出
    # （这是「不再拿强征当锚点」的兜底依据，别让后人把强征加回来）
    paid_only = [itc("征收", 427, 784), itc("征收", 948, 784), itc("征收", 1467, 784),
                 itc("20/强征", 992, 877), itc("20/强征", 1511, 877)]
    check("三格全付费「20/强征」时仍认得出税收面板",
          ui._in_panel("税收", paid_only) is True)

    # ② 原图只读到 1 行时，ocr_multi 要自动补放大 OCR 并合并
    rich = list(real_ss) + [itc("100", 93, 790), itc("659", 372, 790)]

    class _PoorDev:
        """复刻偷读：原图 OCR 只给 1 行。"""
        def ocr(self, tag):
            return [itc("73万6720", 590, 932)], "C:/fake/%s.png" % tag

        def shot_path(self, tag):
            return "C:/fake/%s.png" % tag

    class _RichDev(_PoorDev):
        def ocr(self, tag):
            return list(rich), "C:/fake/%s.png" % tag

    calls = {"n": 0}
    orig = _ui.ocr_image_scaled
    _ui.ocr_image_scaled = lambda path, factor=2, tmp_path=None: (
        calls.__setitem__("n", calls["n"] + 1), list(rich))[1]
    try:
        u2 = FakeUi()
        u2.shots = []
        u2.dev = _PoorDev()
        items, _ = u2.ocr_multi("t", n=1, scaled_fallback=True)
        check("原图只读到 1 行 → 自动补了一轮放大 OCR", calls["n"] == 1)
        check("放大结果并入后能认出税收面板（漏读不再误判）",
              u2._in_panel("税收", items) is True)

        calls["n"] = 0
        u3 = FakeUi()
        u3.shots = []
        u3.dev = _RichDev()
        u3.ocr_multi("t2", n=1, scaled_fallback=True)
        check("原图读得够多 → 不浪费放大 OCR", calls["n"] == 0)

        # 默认关着，不能悄悄改变其它调用点的耗时
        calls["n"] = 0
        u4 = FakeUi()
        u4.shots = []
        u4.dev = _PoorDev()
        u4.ocr_multi("t3", n=1)
        check("默认不开启放大兜底（其它调用点行为不变）", calls["n"] == 0)
    finally:
        _ui.ocr_image_scaled = orig


def test_junqing_and_recruit():
    print("\n[31] 军情批阅战报弹窗 + 招募「免费」误判 / 价格横行读取")
    # ---- ① 「军情批阅」战报弹窗（2026-09-20 实跑发现）----
    # 每次「离开一段时间再回来」游戏都会弹它，所以挂机过夜后的每轮开机必遇到。
    # 它既没有「取消/跳过/关闭」也没有「确定/知道了」，认不出就会一路走到
    # boot 的「认不出的界面」分支、白等 8 轮（实测 ~70 秒）才强制清屏。
    ui = FakeUi()
    ui.tapped = []
    ui.log = lambda *a: None
    ui.tap = lambda x, y: ui.tapped.append((x, y))
    junqing = [
        itc("军情比阅", 319, 200),          # 标题常把「批」读成「比」
        itc("主公，在你离开的4小时", 487, 557),
        itc("军情总览", 1230, 142),
        itc("资源收获", 1230, 593),
        itc("如下事情，请批阅！", 453, 625),   # 正文里也有「批阅」二字
        itc("批阅", 1229, 888),              # 真正的按钮
        itc("今日不再弹出", 1462, 995),       # ★ 绝不勾它
    ]
    sig = ui.guard(junqing)
    check("识别为 junqing", sig == "junqing", repr(sig))
    check("点的是「批阅」按钮，不是复选框",
          ui.tapped == [(1229, 888)], str(ui.tapped))

    # 反例：内政主界面（有「税收/市井」等入口）不该触发
    ui.tapped = []
    nz = [itc("政策", 540, 390), itc("市井", 1045, 700), itc("可领取0/3", 1743, 769)]
    check("内政面板不触发军情批阅", ui.guard(nz) == "", "误触发")
    check("内政面板没被点任何东西", ui.tapped == [], str(ui.tapped))

    # 安全阀：军情批阅的变体上出现消费字样 → 绝不动手
    ui.tapped = []
    danger = [itc("军情总览", 1230, 142), itc("批阅", 1229, 888),
              itc("花费100玉符", 900, 700)]
    check("带消费字样的战报弹窗不动手", ui.guard(danger) == "", "动手了")
    check("消费变体没有发生点击", ui.tapped == [], str(ui.tapped))

    # ---- ② 绿标签「免费」误判：那是付费按钮左边的绿色货币图标 ----
    # 实测（2026-09-20 12:57 半价轮）：按钮 (731,912)，左边 (556,856) 挂着「打折」。
    # 真免费帧的绿标签 40x32/面积 639，付费货币图标 34x40/面积 658 —— 分不开。
    ui2 = FakeUi()
    ui2.log = lambda *a: None
    btn_paid = itc("招募1次", 731, 912)
    paid_items = [btn_paid, itc("打折", 556, 856)]
    check("紧邻「打折」时 → 不认这个绿标签为免费",
          _free_claim_is_trustworthy(ui2, paid_items, btn_paid, False) is False)

    btn_free = itc("招募1次", 1405, 504)
    free_items = [btn_free, itc("免费次数1", 1061, 915)]
    check("真免费帧（近旁没有「打折」）→ 绿标签可信",
          _free_claim_is_trustworthy(ui2, free_items, btn_free, False) is True)

    # ★ 半径不能放大：旁边卡包卡片上的「打折」不能把真免费那轮误杀
    far = [btn_free, itc("打折", 1405 - 430, 504)]      # dx=430 > 240
    check("远处（dx=430）的「打折」不影响真免费帧",
          _free_claim_is_trustworthy(ui2, far, btn_free, False) is True)
    check("红色「打折」丝带同样否掉免费声明（ribbon=True）",
          _free_claim_is_trustworthy(ui2, free_items, btn_free, True) is False)

    # ---- ③ 价格横行读取（卡包详情页：100 / 招募1次）----
    # 价格在这一屏是**横排**的，不在按钮下方，_price_under_btn 读不到；
    # 放大 2 倍后整条读成「》@100/招募1次」。
    b100 = itc("》@100/招募1次", 706, 906)
    check("从「100/招募1次」读出 100", _price_in_btn_row([b100], b100) == 100)
    b950 = itc("《@950/招募5次", 1176, 909)
    check("从「950/招募5次」读出 950", _price_in_btn_row([b950], b950) == 950)
    # ★ 最危险的一种：1x 的「@1佣/招募1次」里那个「1」绝不能被当成价格
    #   （读成 1 比读不出来更糟 —— 「价格 ≤ 上限」会永远通过）
    b_garbled = itc("@1佣/招募1次", 731, 912)
    check("1x 的「@1佣/招募1次」读不出价格（绝不能读成 1）",
          _price_in_btn_row([b_garbled], b_garbled) is None)
    b_bare = itc("招募1次", 700, 900)
    check("裸「招募1次」不会被读成价格",
          _price_in_btn_row([b_bare], b_bare) is None)
    # 离得太远的数字不认（避免吃到页面别处的价格）
    far_num = itc("》@100/招募1次", 700 + 500, 906)
    check("远处（dx=500）的价格不算在按钮头上",
          _price_in_btn_row([far_num], itc("招募1次", 700, 906)) is None)


def test_current_role():
    print("\n[32] 读「游戏内角色名」（按角色名识别的身份判据）")
    # 2026-09-20 实测结论：游戏里**只有区服可选，角色名不出现在登录链路上**，
    # 角色名只出现在进游戏后的主城左上角（「势力值」正上方那一行）。
    # 所以「按角色名识别」= 切过去之后回读这一行做校验。
    import stzb.account as _A

    # 帧来自真实主城 OCR：角色名在「势力值」上方，右侧还有一堆资源计数
    frame = [
        itc("云魇丨奈子", 376, 19),          # ← 真角色名
        itc("·19／35", 549, 19),
        itc("酽一木+1675、、铁+2485", 849, 20),
        itc("@400亞", 1464, 35),             # ← 曾经被误读成角色名的那个
        itc("亞0148万", 1783, 35),
        itc("．18：24：31", 559, 49),
        itc("》忄势力值154", 297, 51),        # ← 锚点
        itc("任务", 58, 109),
    ]

    class _U:
        def __init__(self, items):
            self._items = items

        def ocr(self, tag="x"):
            return self._items, ""

    got = _A.current_role(_U(frame))
    check("从主城读到角色名「云魇丨奈子」", got == "云魇丨奈子", repr(got))
    check("★ 不会被右上角的资源计数抢走（曾经读成「400亞」）", got != "400亞", repr(got))

    # 名字与「势力值」被 OCR 并成一条时，也要能抠出来
    merged = [itc("云魇丨奈子势力值154", 380, 40)]
    check("名字与「势力值」并成一条时能抠出来",
          _A.current_role(_U(merged)) == "云魇丨奈子", repr(_A.current_role(_U(merged))))

    # 登录页/面板上读不到 → 必须返回 None（绝不瞎猜）
    login = [itc("网易游戏", 1019, 300), itc("常用", 1340, 432),
             itc("159****4508", 807, 459), itc("登录", 959, 683),
             itc("其他账号登录", 960, 813)]
    check("登录页上读不到角色名 → 返回 None",
          _A.current_role(_U(login)) is None, repr(_A.current_role(_U(login))))
    check("空屏幕 → 返回 None", _A.current_role(_U([])) is None)

    # 装饰噪声前缀要清掉（只剥非字母数字/非汉字的标点；
    # 「忄」是 CJK 部首，属汉字区，按设计保留）
    check("剥掉角色名前的标点装饰",
          _A._clean_role_name("》忄势力值") == "忄势力值",
          repr(_A._clean_role_name("》忄势力值")))
    check("剥掉纯标点前缀/后缀",
          _A._clean_role_name("·云魇丨奈子·") == "云魇丨奈子",
          repr(_A._clean_role_name("·云魇丨奈子·")))
    # 「像名度」：中文多才算名字，右栏计数（纯数字）必须被压下去
    check("「云魇丨奈子」的像名度 > 「400亞」",
          _A._name_likeness("云魇丨奈子") > _A._name_likeness("400亞"))
    check("右栏计数「19／35」不像名字（像名度 <= 0）",
          _A._name_likeness("19／35") <= 0, repr(_A._name_likeness("19／35")))

    # ---- 登录链路的两屏必须分得开（这是「切不了账号」的根因所在）----
    # 「点击换区」只在**游戏自己的登录页**上；网易统一登录页上没有它。
    check("网易登录页不算「游戏登录页」（没有点击换区）",
          not _A._has(login, *_A.KW_START_GAME)
          and not _A._has(login, *_A.KW_AREA_ENTRY))
    game_login = [itc("三国率土之滨", 640, 200), itc("X6014 龙兴之地", 500, 430),
                  itc("点击换区", 400, 430), itc("开始游戏", 512, 490)]
    check("游戏登录页能认出来（有开始游戏 + 点击换区）",
          _A._has(game_login, *_A.KW_START_GAME)
          and _A._has(game_login, *_A.KW_AREA_ENTRY))
    # ⚠️ 主城上 (60,62) 是「任务」按钮，不是用户中心入口 —— 这条必须记住
    check("角色名锚点用的是「势力值」，不是左上角那个位置",
          _A.ROLE_NAME_ANCHOR == "势力值")


def test_run_liveness():
    """「本机是否正在跑一轮」的判据（常驻代理与 run_daily 共用）。

    判错的两个方向都会出事：
      · 把空闲判成忙  → 后台派的任务永远接不了（提示「已有一轮在跑」但其实是残留锁）；
      · 把忙判成空闲  → 两个进程一起点模拟器，点击全部错位，还可能重复领奖。
    """
    print("\n[34] 本机运行状态判据")
    import datetime as _dt
    import json as _json
    import subprocess
    import tempfile

    from stzb.identity import (LOCK_STALE_SECONDS, pid_alive, run_in_progress,
                               run_lock_path)

    def _write_lock(path, pid, started):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({"pid": pid, "started": started}, f)

    def _now_iso():
        return _dt.datetime.now().isoformat(timespec="seconds")

    root = tempfile.mkdtemp(prefix="stzb_live_")
    lock = run_lock_path(root)

    # 没有锁文件 → 空闲
    check("没有锁文件 → 不算在跑", run_in_progress(root) is False)

    # 锁里写的是**自己**的 pid → 不算「别人在跑」
    _write_lock(lock, os.getpid(), _now_iso())
    check("锁里是自己的 pid → 不算在跑（避免自锁）", run_in_progress(root) is False)

    # ★ 残留锁（pid 早就不在了）→ 必须判成**空闲**，否则任务永远接不了
    _write_lock(lock, 999999, _now_iso())
    check("pid 已不在（残留锁）→ 判空闲，不是忙", run_in_progress(root) is False)

    # pid 活着但锁已超龄 → 也当残留
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        old = _dt.datetime.now() - _dt.timedelta(seconds=LOCK_STALE_SECONDS + 60)
        _write_lock(lock, p.pid, old.isoformat(timespec="seconds"))
        check("pid 活着但锁超龄 → 判空闲", run_in_progress(root) is False)

        # pid 活着且锁是新鲜的 → 这才算「真有一轮在跑」
        _write_lock(lock, p.pid, _now_iso())
        check("pid 活着 + 锁新鲜 → 判「正在跑」", run_in_progress(root) is True)
        check("pid_alive 对活着的进程返回 True", pid_alive(p.pid) is True)
    finally:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass
    check("pid_alive 对不存在的 pid 返回 False", pid_alive(999999) is False)

    # 锁文件内容坏掉（半截 JSON / 空文件）→ 判空闲，绝不抛异常
    with open(lock, "w", encoding="utf-8") as f:
        f.write("{ 坏掉的内容")
    check("锁文件内容坏掉 → 判空闲且不抛异常", run_in_progress(root) is False)
    with open(lock, "w", encoding="utf-8") as f:
        f.write("")
    check("锁文件为空 → 判空闲", run_in_progress(root) is False)

    # 常驻代理起 run_daily 时必须带 --job-only（否则队列空就会误跑一轮常规任务）
    with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as f:
        agent_src = f.read()
    check("常驻代理起 run_daily 时带上了 --job-only",
          '--job-only' in agent_src, "agent.py 里找不到 --job-only")
    with open(os.path.join(ROOT, "run_daily.py"), encoding="utf-8") as f:
        rd_src = f.read()
    check("run_daily 实现了 --job-only 且队列空时直接退出（不退化成常规运行）",
          "args.job_only and not job" in rd_src and "job-only" in rd_src)
    check("run_daily 与常驻代理共用同一套存活判据（不各自实现一遍）",
          "from stzb.identity import" in rd_src and "run_in_progress" in agent_src
          and "def pid_alive" not in rd_src)



def test_role_dialog():
    print("\n[33] 「选择角色」对话框：按角色名识别 / 点选")
    # ★★★ 2026-09-20 实机发现：这才是「按角色名识别」唯一可靠的地方。
    # 「选择服务器」面板里全是区服名（X6014龙兴之/备战区/S21815…），
    # 真正的角色列表在**点开始游戏之后**弹的这个对话框里。
    # 下面四条文本/坐标抄自真实帧 bz_go_19_020.png。
    import stzb.account as _A

    dlg = [
        itc("选择角色", 961, 231),      # 标题
        itc("执剑丨青山", 960, 362),     # 条目 1
        itc("鸡波长", 959, 462),        # 条目 2
        itc("确定", 960, 818),          # 按钮
    ]

    class _U:
        def __init__(self, items):
            self._items = items
            self.tapped = []

        def ocr(self, tag="x"):
            return self._items, ""

        def tap(self, x, y, delay=0.7):
            self.tapped.append((x, y))

        def log(self, msg):
            pass

    u = _U(dlg)
    check("认出这是「选择角色」对话框", _A.is_role_dialog(u, dlg) is True)
    check("列出角色名（标题和「确定」不算条目）",
          _A.list_role_dialog(u, dlg) == ["执剑丨青山", "鸡波长"],
          repr(_A.list_role_dialog(u, dlg)))
    check("条目按屏幕从上到下排序",
          [e.center[1] for e in _A._role_dialog_entries(dlg)] == [362, 462],
          repr([e.center[1] for e in _A._role_dialog_entries(dlg)]))

    # 不在这一屏时必须如实说「不在」，不能拿别处的文字硬凑
    home = [itc("云魇丨奈子", 376, 19), itc("势力值154", 297, 51)]
    check("主城帧不算「选择角色」对话框", _A.is_role_dialog(_U(home), home) is False)
    check("不在对话框上时列出空列表", _A.list_role_dialog(_U(home), home) == [])

    ok, why = _A.pick_role_dialog(_U(home), "执剑丨青山", home)
    check("不在对话框上时点选必须失败并说明原因", ok is False and "不在" in why,
          "%r %r" % (ok, why))

    # 目标角色不在列表里 → 如实报错，并把现有角色列出来（绝不瞎点一个）
    u2 = _U(dlg)
    ok2, why2 = _A.pick_role_dialog(u2, "张三", dlg)
    check("找不到目标角色时不点任何东西",
          ok2 is False and u2.tapped == [], "%r %r" % (ok2, u2.tapped))
    check("报错里带上现有角色名，便于用户核对",
          "执剑丨青山" in why2 and "鸡波长" in why2, repr(why2))

    # 同名歧义 → 不猜（用两条同名的条目，这是最容易翻车的场景）
    amb = [itc("选择角色", 961, 231),
           itc("执剑丨青山", 960, 362), itc("执剑丨青山", 960, 462),
           itc("确定", 960, 818)]
    u3 = _U(amb)
    ok3, why3 = _A.pick_role_dialog(u3, "执剑丨青山", amb)
    check("两条同名时不猜（点错=在别人号上跑任务）",
          ok3 is False and u3.tapped == [], "%r %r" % (ok3, u3.tapped))

    # 正向：唯一命中时才会真的去点，并点条目 + 确定两下
    uni = [itc("选择角色", 961, 231),
           itc("执剑丨青山", 960, 362), itc("鸡波长", 959, 462),
           itc("确定", 960, 818)]
    u4 = _U(uni)
    ok4, why4 = _A.pick_role_dialog(u4, "鸡波长", uni)
    check("唯一命中时点中该条目并点确定",
          ok4 is True and u4.tapped == [(959, 462), (960, 818)],
          "%r %r" % (ok4, u4.tapped))


def test_role_vert_norm():
    """角色名里的「竖线类字符」必须先统一，再比较。

    实测背景（2026-09-20 实机）：游戏角色名大量用中文竖线「丨」(U+4E28)
    （「执剑丨青山」「云魇丨奈子」），连续三次 OCR 都稳定读出 U+4E28。
    而**人在后台手打时几乎必然打成 ASCII 竖线 `|`** —— 两个码点长得几乎一样。
    不统一的后果：同一个角色被判成两个人 → 自动发现重复建角色、
    指派比对永远不等 → 天天白切一遍（正是「只认角色名」要防的事）。
    """
    print("\n[35] 角色名竖线归一化（手打 `|` ≡ OCR 读出的「丨」）")
    import stzb.account as _A

    # 各种竖线写法都要收敛
    for a, b in [("执剑丨青山", "执剑|青山"), ("执剑丨青山", "执剑｜青山"),
                 ("云魇丨奈子", "云魇|奈子"), ("云魇丨奈子", "云魇│奈子")]:
        check("「%s」≡「%s」" % (a, b), _A._one_score(a, b) == 1.0,
              "score=%.3f" % _A._one_score(a, b))

    # 归一化后的形状统一成中文竖线
    check("_clean_role_name 把 ASCII 竖线统一成「丨」",
          _A._clean_role_name("执剑|青山") == "执剑丨青山",
          repr(_A._clean_role_name("执剑|青山")))

    # ★ 反向护栏：绝不能把真正的字母/数字也归一化掉
    for bad in "Il1":
        check("竖线字符表里不含字母/数字 %r（否则会误伤真名字）" % bad,
              bad not in _A._VERT_CHARS)
    check("「Iron」与「lron」仍是两个不同名字",
          _A._one_score("Iron", "lron") < 0.99,
          "score=%.3f" % _A._one_score("Iron", "lron"))


def test_device_target():
    """指定目标设备（--serial / STZB_DEVICE_SERIAL）—— 「在手机上跑一轮」的能力。

    ★ 为什么进自检（2026-09-21）：
      多设备上线后，`run_daily` 原来只能靠 `config.device.serial_candidates` 猜。
      而实体机 serial（`340436524100AJ8`，**没有冒号**）天生不在那份候选表里，
      于是"指定在手机上跑"这件事**根本没法表达** —— 后果不是报错，
      而是**悄悄连到模拟器上跑**（在别的设备上跑 = 在别的号上花资源）。
      这类「跑到别的设备上」是本项目最不能接受的一类错误，所以把判据钉死。
    """
    print("\n[36] 指定目标设备：只连这一台，绝不退回别的设备")
    import subprocess

    # ① 形态判据：实体机没有冒号，模拟器一定有
    from stzb import devices as _D
    from run_daily import _cand, _emu_shape   # noqa: PLC0415

    check("实体机 serial 判为「非模拟器」", _emu_shape("340436524100AJ8") is False)
    check("host:port 判为模拟器", _emu_shape("127.0.0.1:7555") is True)
    check("emulator-N 判为模拟器", _emu_shape("emulator-5554") is True)
    check("空 serial 按老规矩当模拟器（不改变老行为）", _emu_shape("") is True)
    # 与 stzb.devices 里的判据必须同一套口径，不能各写一份
    check("与 stzb.devices 的模拟器形态判据一致",
          bool(_D._EMU_SERIAL_RE.match("127.0.0.1:7555"))
          and not _D._EMU_SERIAL_RE.match("340436524100AJ8"))

    # ② 候选表：指定目标后**只给一台**（不给退路）
    class _Cfg(dict):
        def get(self, k, d=None):
            if k == "device.serial_candidates":
                return ["127.0.0.1:7555", "127.0.0.1:16384"]
            return d

    import run_daily as _rd
    old = _rd.TARGET_SERIAL
    try:
        _rd.TARGET_SERIAL = ""
        check("未指定目标 → 用配置里的候选表",
              _cand(_Cfg()) == ["127.0.0.1:7555", "127.0.0.1:16384"])
        _rd.TARGET_SERIAL = "340436524100AJ8"
        c = _cand(_Cfg())
        check("指定目标 → 候选表只剩它一个（没有退路）",
              c == ["340436524100AJ8"], str(c))
    finally:
        _rd.TARGET_SERIAL = old

    # ③ 真实命令行：--serial 必须真的接上（项目教训：只测函数测不到「参数有没有接上」）
    p = subprocess.run([sys.executable, os.path.join(ROOT, "run_daily.py"),
                        "--list", "--serial", "340436524100AJ8"],
                       capture_output=True, text=True, timeout=60,
                       cwd=ROOT, errors="ignore")
    out = (p.stdout or "") + (p.stderr or "")
    check("--serial 走真实命令行被识别为实体机",
          "340436524100AJ8" in out and "实体机" in out,
          out.strip().splitlines()[:1])

    # ④ 启动段与收尾段必须共用同一个「要不要管模拟器」——
    #    两处各写一遍的话，在手机上跑完会把用户的模拟器关掉。
    with open(os.path.join(ROOT, "run_daily.py"), encoding="utf-8") as f:
        src = f.read()
    check("启动/收尾共用 manage_emu（收尾不再自己写一遍条件）",
          "manage_emu = not args.no_emulator" in src
          and "if manage_emu:" in src
          and "if not args.no_emulator:\n        if shutdown_after" not in src)
    # ⑤ 画面/分辨率不对齐时**必须拒绝点击**（坐标写死 1920×1080，错分辨率=乱点）。
    #    实体机走 `ensure_frame_1920x1080`（见第 38 组），模拟器仍用 force_landscape。
    check("画面不对齐时拒绝跑（安全前置）",
          'ensure_frame_1920x1080(' in src and "未做任何点击" in src)
    check("模拟器路径保留 force_landscape（实体机走自适应那条）",
          "force_landscape" in src and "_di.needs_landscape" in src)


def test_awake_guard():
    """实体机「亮屏 / 防息屏 / 锁屏拒绝」的判据（2026-09-21 新增）。

    ★ 为什么必须有这一组：
      模拟器的屏幕永远不会睡，所以这条路径在模拟器时代从来没被需要过 ——
      它是**只有真机才会暴露**的一类问题：手机在任务中途息屏（顺带还会锁屏），
      `screencap` 拿到黑屏，OCR 一行都读不出来，脚本于是在**锁屏界面上**
      点满一整轮，最后报一个跟真实原因毫不相干的「进不去某界面」。
      实测（vivo V2055A）确认：息屏后 `screen_locked` 立刻变 True。

    这里用**替换 `_adb_run` 返回假 dumpsys 文本**的方式测解析层 ——
    解析正是最容易写错的地方（`mCurrentFocus` 在 dumpsys 里出现两次，
    只取第一行会永远读到 null，把「锁着」判成「没锁」）。
    """
    print("\n[37] 实体机亮屏/防息屏/锁屏判据")
    import stzb.devices as _D

    # ① 位值：USB = 2。写错成 1（AC）就会去改错的那一位，等于没防住息屏。
    check("STAY_ON_USB 位值 = 2（USB）", _D.STAY_ON_USB == 2, str(_D.STAY_ON_USB))

    orig = _D._adb_run
    try:
        def _mk(power="", policy="", window=""):
            def fake(adb, args, timeout=20.0):
                a = " ".join(args)
                if "dumpsys power" in a:
                    return power
                if "dumpsys window policy" in a:
                    return policy
                if "dumpsys window" in a:
                    return window
                return ""
            return fake

        # ② 亮/暗
        _D._adb_run = _mk(power="  mWakefulness=Awake\n")
        check("mWakefulness=Awake → awake", _D.screen_state("adb", "s") == "awake")
        _D._adb_run = _mk(power="  mWakefulness=Asleep\n")
        check("mWakefulness=Asleep → asleep", _D.screen_state("adb", "s") == "asleep")
        _D._adb_run = _mk(power="  mWakefulness=Dozing\n")
        check("mWakefulness=Dozing → asleep", _D.screen_state("adb", "s") == "asleep")
        _D._adb_run = _mk(power="完全读不到\n")
        check("读不到 mWakefulness → unknown（绝不猜）",
              _D.screen_state("adb", "s") == "unknown")

        # ③ 锁屏：首选 KeyguardStateMonitor 段下的 mIsShowing
        _D._adb_run = _mk(policy="  KeyguardStateMonitor\n    mIsShowing=true\n")
        check("KeyguardStateMonitor.mIsShowing=true → 锁着",
              _D.screen_locked("adb", "s") is True)
        _D._adb_run = _mk(policy="  KeyguardStateMonitor\n    mIsShowing=false\n")
        check("KeyguardStateMonitor.mIsShowing=false → 没锁",
              _D.screen_locked("adb", "s") is False)

        # ④ ★ 回归：mCurrentFocus 在 dumpsys window 里出现**两次**，
        #    第 151 行是 null、第 253 行才是真窗口。只取第一行 → 永远判「没锁」
        #    → 在锁屏上瞎点。必须取**最后一行**。
        two_focus_locked = (
            "  mCurrentFocus=null\n"
            "  其它内容\n"
            "  mCurrentFocus=Window{abc u0 NotificationShade/Keyguard}\n")
        _D._adb_run = _mk(window=two_focus_locked)
        check("两行 mCurrentFocus（先 null 后 Keyguard）→ 判锁着（取最后一行）",
              _D.screen_locked("adb", "s") is True)

        two_focus_free = (
            "  mCurrentFocus=null\n"
            "  mCurrentFocus=Window{abc u0 com.netease.stzb.netease/Client}\n")
        _D._adb_run = _mk(window=two_focus_free)
        check("两行 mCurrentFocus（先 null 后游戏）→ 判没锁",
              _D.screen_locked("adb", "s") is False)

        # ⑤ 两条路都读不到 → None（未知），绝不能默认成「没锁」
        _D._adb_run = _mk()
        check("完全读不到锁屏状态 → None（未知，不猜 False）",
              _D.screen_locked("adb", "s") is None)

        # ⑥ ensure_awake：息屏+锁屏 → 先唤醒、再如实拒绝。
        #    ★ 假 adb 必须**有状态**：发过 KEYCODE_WAKEUP 之后要变成 Awake，
        #      否则测的是「唤不醒」那条分支，而不是「唤醒了但仍锁着」。
        st = {"awake": False, "woke": False}

        def fake_locked(adb, args, timeout=20.0):
            a = " ".join(args)
            if "dumpsys power" in a:
                return "  mWakefulness=%s\n" % ("Awake" if st["awake"] else "Asleep")
            if "KEYCODE_WAKEUP" in a:
                st["woke"] = True
                st["awake"] = True                # 模拟真机被唤醒
                return ""
            if "dumpsys window policy" in a:
                return "  KeyguardStateMonitor\n    mIsShowing=true\n"
            return ""
        _D._adb_run = fake_locked
        r = _D.ensure_awake("adb", "s", logger=lambda m: None)
        check("ensure_awake 认出了「原本息屏」", r.get("state") == "awake", str(r))
        check("确实发了 KEYCODE_WAKEUP", st["woke"] is True)
        check("息屏+锁屏 → ok=False 且提示去解锁",
              r["ok"] is False and "解锁" in (r.get("message") or ""), str(r.get("message")))

        # ⑦ ensure_awake：亮屏 + 已设常亮 → ok=True 且**不改任何设置**
        _D._adb_run = _mk(power="  mWakefulness=Awake\n",
                          policy="  KeyguardStateMonitor\n    mIsShowing=false\n")
        # settings get 返回 7（已含 USB 位）
        _orig2 = _D._adb_run
        calls = []

        def fake2(adb, args, timeout=20.0):
            a = " ".join(args)
            calls.append(a)
            if "stay_on_while_plugged_in" in a and "get" in a:
                return "7\n"
            return _orig2(adb, args, timeout)
        _D._adb_run = fake2
        r2 = _D.ensure_awake("adb", "s", logger=lambda m: None)
        check("状态正常时 ok=True", r2["ok"] is True, str(r2.get("message")))
        check("已含 USB 位 → changed=False（不动用户设置）", r2["changed"] is False)
        check("没有发出任何 settings put（不改用户设置）",
              not any("settings put" in c for c in calls), str(calls))

        # ⑧ 缺 USB 位 → 补上，并把**原值**带回去供还原
        calls2 = []

        def fake3(adb, args, timeout=20.0):
            a = " ".join(args)
            calls2.append(a)
            if "stay_on_while_plugged_in" in a and "get" in a:
                return "1\n"                      # 只有 AC 位，缺 USB
            return _orig2(adb, args, timeout)
        _D._adb_run = fake3
        r3 = _D.ensure_awake("adb", "s", logger=lambda m: None)
        check("缺 USB 位 → changed=True 且带回原值 1",
              r3["changed"] is True and r3["stay_on_before"] == 1, str(r3))
        check("补的值 = 原值 | 2（只补 USB 位，不动别的位）",
              r3["stay_on_after"] == 3, str(r3.get("stay_on_after")))
        check("确实发出了 settings put",
              any("settings put" in c for c in calls2), str(calls2))
    finally:
        _D._adb_run = orig

        # ⑨ 接线检查：run_daily 必须在**跑之前**做这两件事，且失败要拒绝点击
    with open(os.path.join(ROOT, "run_daily.py"), encoding="utf-8") as f:
        src = f.read()
    check("run_daily 对实体机做 ensure_awake", "ensure_awake(" in src)
    check("锁屏/屏幕不可用 → _bail（不点击）",
          'aw.get("ok")' in src and "未做任何点击" in src)
    check("跑完还原插电常亮设置（restore_stay_on）", "restore_stay_on(" in src)
    check("还原在 _bail 里也有（早期失败不能留下被改过的设置）",
          src.count("restore_stay_on(") >= 2, "出现 %d 次" % src.count("restore_stay_on("))
    check("_adb 在任何 _bail 之前初始化（否则早期失败路径 NameError）",
          src.index("_adb = cfg.get") < src.index("manage_emu = not args.no_emulator"))


def test_frame_fit():
    """横屏画面判据：**实测画面**才是唯一可信的判据（2026-09-21 真机抓到的 bug）。

    ★ 事故回顾（第 3 轮跑批）：手机被放成竖的，`wm size` 仍回显
      `Override size: 1080x1920`（那个字符串不随旋转变），于是「只比 override」
      的检查通过了 → 游戏按 1080×1920 竖屏排版 → 写死的坐标全部错位 →
      脚本卡在「认不出的界面」连点 14 次 ✕、整轮 **成功 1 / 失败 5、
      白跑 842 秒、留下 250+ 张废截图**，而日志里一个字都没提方向。

    修法两条，都要钉住：
      ① 判据改成**截一帧读 PNG 头**（`frame_size`），不是读 `wm size` 的回显；
      ② 覆盖值不再写死 —— 两个候选都试，留那个真能出 1920×1080 的
         （因为「该传哪个」取决于当前旋转，而旋转读不准：vivo/OriginOS
         忽略 ADB 设的旋转锁，实测连桌面都不跟着转）。
    """
    print("\n[38] 横屏画面：实测画面为判据 + 覆盖值自适应")
    import io
    import struct as _struct
    import stzb.devices as _D

    # ① 常量：画面尺寸与候选值
    check("目标画面 1920x1080", (_D.FRAME_W, _D.FRAME_H) == (1920, 1080))
    check("候选里同时有 1080x1920 与 1920x1080（两个方向都要试）",
          set(_D.OVERRIDE_CANDIDATES) == {"1080x1920", "1920x1080"},
          str(_D.OVERRIDE_CANDIDATES))

    # ② frame_size 必须真的**解析截图字节**，而不是去读 wm size。
    #    用一个手搓的最小 PNG 头验证解析（含"必须按大端读"这条）。
    def _png(w, h):
        return (b"\x89PNG\r\n\x1a\n" + _struct.pack(">I", 13) + b"IHDR"
                + _struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"\x00" * 8)

    orig_run = _D.subprocess.run
    orig_adb = _D._adb_run

    class _P:
        def __init__(self, out):
            self.stdout = out

    try:
        _D.subprocess.run = lambda *a, **k: _P(_png(1920, 1080))
        check("frame_size 从 PNG 头读出 1920x1080（大端）",
              _D.frame_size("adb", "s") == (1920, 1080))
        _D.subprocess.run = lambda *a, **k: _P(_png(1080, 1920))
        check("竖屏截图读成 1080x1920（不会被当横屏）",
              _D.frame_size("adb", "s") == (1080, 1920))
        _D.subprocess.run = lambda *a, **k: _P(b"not a png at all")
        check("不是 PNG → None（不猜）", _D.frame_size("adb", "s") is None)
        _D.subprocess.run = lambda *a, **k: _P(b"")
        check("空输出 → None（不猜）", _D.frame_size("adb", "s") is None)

        # ③ 覆盖值自适应：模拟「物理竖屏的手机」——
        #    只要 override 不是 1920x1080，画面就是竖的 1080×1920；
        #    一旦覆盖成 1920x1080，画面才变横屏。必须挑出后者，
        #    且**先试的那个（1080x1920）失败不算错**。
        st = {"override": "", "ar": "1"}
        applied = []

        def fake_adb(adb, args, timeout=20.0):
            a = " ".join(args)
            # 系统「自动旋转」开关（失败诊断会读它）
            if "accelerometer_rotation" in a:
                return st["ar"] + "\n"
            if "wm size" in a:
                parts = a.split()
                if parts[-1] == "size":          # 读：回显 Physical + Override
                    s = "Physical size: 1080x2400\n"
                    if st["override"]:
                        s += "Override size: %s\n" % st["override"]
                    return s
                # 写：wm size <值> / wm size reset
                st["override"] = "" if parts[-1] == "reset" else parts[-1]
                applied.append(parts[-1])
            return ""

        def fake_shot(*a, **k):
            # 竖屏手机：只有覆盖成 1920x1080 才出横屏画面
            return _P(_png(1920, 1080) if st["override"] == "1920x1080"
                      else _png(1080, 1920))

        _D._adb_run = fake_adb
        _D.subprocess.run = fake_shot
        r = _D.ensure_frame_1920x1080("adb", "s", logger=lambda m: None)
        check("竖屏手机上自动挑出能出横屏的那个覆盖值",
              r["ok"] is True and r["override"] == "1920x1080", str(r))
        check("确实试过两个候选（不是只试一个就放弃）",
              applied[:2] == ["1080x1920", "1920x1080"], str(applied))

        # ④ 两个都试不出横屏 → ok=False（交给调用方拒绝跑），且提示可操作
        st.update(override="1080x1920", ar="1")
        applied2 = []
        _D._adb_run = fake_adb
        _D.subprocess.run = lambda *a, **k: _P(_png(1080, 1920))
        r2 = _D.ensure_frame_1920x1080("adb", "s", logger=lambda m: None)
        check("两个候选都拿不到横屏 → ok=False", r2["ok"] is False, str(r2))
        check("拒绝时给出可操作提示（把手机横过来 / 自动旋转）",
              "横过来" in (r2.get("message") or ""), str(r2.get("message")))
        # ★ 失败必须把 override 还原成**进来时的样子**：
        #   上面每个候选都真的写过一次 `wm size`，不还原就停在最后一个候选上，
        #   用户拔下手机自己用会发现界面尺寸莫名变了，还不知道是谁改的。
        check("失败时把 override 还原成进入时的值",
              st["override"] == "1080x1920", "还原后=%r" % st["override"])

        # ④b 自动旋转关着 → 提示必须点破这一点
        #     （这是「手机横过来了但画面还是竖的」的唯一常见原因，
        #       不说破用户不知道下一步该动哪里）
        st.update(override="1080x1920", ar="0")
        _D._adb_run = fake_adb
        _D.subprocess.run = lambda *a, **k: _P(_png(1080, 1920))
        r2b = _D.ensure_frame_1920x1080("adb", "s", logger=lambda m: None)
        check("自动旋转关闭时，提示明确说「自动旋转是关闭的」",
              "自动旋转" in (r2b.get("message") or "")
              and "关闭" in (r2b.get("message") or ""), str(r2b.get("message")))

        # ⑤ 已经是横屏 → 一个字节都不改（不改用户设备状态）
        applied3 = []

        def fake_adb3(adb, args, timeout=20.0):
            a = " ".join(args)
            if "wm size" in a:
                parts = a.split()
                if parts[-1] == "size":
                    return "Physical size: 1080x2400\n"
                applied3.append(a)
            return ""

        _D._adb_run = fake_adb3
        _D.subprocess.run = lambda *a, **k: _P(_png(1920, 1080))
        r3 = _D.ensure_frame_1920x1080("adb", "s", logger=lambda m: None)
        check("画面已经对 → ok=True 且不写任何 wm size",
              r3["ok"] is True and not applied3, str(applied3))
    finally:
        _D.subprocess.run = orig_run
        _D._adb_run = orig_adb

    # ⑥ 接线：实体机走这条自适应路径，且失败要拒绝跑
    with io.open(os.path.join(ROOT, "run_daily.py"), encoding="utf-8") as f:
        src = f.read()
    check("run_daily 对实体机调 ensure_frame_1920x1080", "ensure_frame_1920x1080(" in src)
    check("画面不对 → _bail（不点击）",
          '_fk.get("ok")' in src and "未做任何点击" in src)


def test_home_tabs():
    """主城底部页签：几何定位（不靠 OCR、不靠固定坐标）。

    ★ 事故回顾（2026-09-21 真机）：底部那排 武将/库藏/内政/势力/同盟 是
      **竖排书法体，OCR 读不出来**，所以老代码盲点固定坐标 `(412,942)`。
      而那个坐标只在模拟器上成立 —— 它其实**只是勉强落在内政页签的右边缘**：
        模拟器实测页签中心 167/275/**388**/495/608（内政块 x≈345~431，412 在边内）
        手机实测页签中心   153/254/**358**/458/561（内政块 x≈315~401，412 已在块外）
      于是手机上「打开内政」把**势力**面板打开了 → 内政下 4 个任务全废，
      而日志只写「打不开内政面板」，完全指不到真正的原因。

    修法：在底部找到那 5 个白块（形状指纹干净、与分辨率无关），
    按 x 排序取第 3 个。这里用**合成图**验证，不依赖 logs/ 里的历史截图。
    """
    print("\n[39] 主城底部页签：几何定位（替代设备相关的固定坐标）")
    import numpy as _np
    from stzb.ui import (HOME_TAB_NEIZHENG, HOME_TAB_NEIZHENG_INDEX,
                         HOME_TAB_ORDER, Ui)

    check("页签顺序固定为 武将/库藏/内政/势力/同盟",
          HOME_TAB_ORDER == ("武将", "库藏", "内政", "势力", "同盟"),
          str(HOME_TAB_ORDER))
    check("「内政」是第 3 个（index=2）",
          HOME_TAB_ORDER[HOME_TAB_NEIZHENG_INDEX] == "内政")
    check("老固定坐标仍保留作最后兜底（模拟器时代的行为不删）",
          isinstance(HOME_TAB_NEIZHENG, tuple) and len(HOME_TAB_NEIZHENG) == 2)

    ui = Ui.__new__(Ui)                       # 只要方法，不跑 __init__

    def _mk_img(centers):
        """在 1920x1080 黑底上按实测尺寸画 5 个白块（宽85 高183）。"""
        img = _np.zeros((1080, 1920, 3), dtype=_np.uint8)
        for cx, cy in centers:
            x0, y0 = int(cx - 42), int(cy - 91)
            img[y0:y0 + 183, x0:x0 + 85] = (255, 255, 255)
        return img

    # ① 手机实测位置 → 必须定位到 358，而不是 412（那正是点错的坐标）
    phone = [(153, 957), (254, 955), (358, 964), (458, 954), (561, 957)]
    b = ui.home_tab_centers(_mk_img(phone))
    check("认出 5 个页签", len(b) == 5, str(b))
    check("按 x 从左到右排序", b == sorted(b), str(b))
    pt = ui._home_tab_point("内政", _mk_img(phone))
    check("手机上「内政」定位到 x=358（不是老的 412）", pt == (358, 964), str(pt))
    check("老常量 412 确实不在「内政」块内（这正是 bug 的原因）",
          not (358 - 42 <= HOME_TAB_NEIZHENG[0] <= 358 + 42),
          "老 x=%d，内政块 316~400" % HOME_TAB_NEIZHENG[0])

    # ② 模拟器实测位置（整排右移）→ 定位到 388，说明是**自适应**而不是写死
    emu = [(167, 947), (275, 945), (388, 956), (495, 962), (608, 947)]
    check("模拟器位置上「内政」定位到 x=388",
          ui._home_tab_point("内政", _mk_img(emu)) == (388, 956))

    # ③ 页签没露出来 / 数量不对 → 返回 None（交给调用方换一帧重试，绝不猜序号）
    check("全黑帧 → 定位不到（None）", ui._home_tab_point("内政", _mk_img([])) is None)
    only_three = [(153, 957), (254, 955), (358, 964)]
    check("只找到 3 个块 → 不按序号猜（None）",
          ui._home_tab_point("内政", _mk_img(only_three)) is None)

    # ④ 接线：open_neizheng 必须**先用几何、后回退**，且回退前先换帧重试
    with open(os.path.join(ROOT, "stzb", "ui.py"), encoding="utf-8") as f:
        src = f.read()
    check("open_neizheng 调了 _home_tab_point", "_home_tab_point(\"内政\"" in src)
    check("定位不到时先换一帧重试，不直接盲点固定坐标",
          "换一帧重试，不盲点" in src)


def test_back_arrow():
    """面板右上角「返回箭头」：几何定位（跨设备）。

    ★ 事故回顾（2026-09-21 真机）：关面板用的那串固定候选点是**在模拟器上量的**
      （`SUB_CLOSE`/`NZ_BACK` 都在 (1832,53)/(1835,57) 一带）。而实体机的应用可用区
      不是满屏（实测 `mAppBounds=Rect(0,36-1920,954)`，只有 1920×918，
      被刘海内边距 + 状态栏挤掉一截），游戏把整个 UI **重新排版**：
        模拟器箭头中心 **(1832, 53)**  ← 老候选正好命中
        手机箭头中心   **(1759, 132)**  ← 老候选全部落空
      后果：手机上关不掉内政面板，`to_home()` 连试 14 轮全失败、卡 ~5 分钟，
      日志里只有一长串「尝试关闭当前面板 政策」，完全看不出真正原因。

    修法：按**颜色+形状**几何定位（实测两台均值 BGR 都是 (140,189,205)、
    块 53~58×39~44，且右上角该区域内**有且只有一个**这种色块）。
    """
    print("\n[40] 返回箭头：几何定位（替代设备相关的固定坐标）")
    import numpy as _np
    from stzb.ui import BACK_ARROW_BOX, Ui

    ui = Ui.__new__(Ui)

    def _mk(blobs):
        """暗底 1920x1080 上画奶油色块（BGR 与实测一致）。"""
        img = _np.zeros((1080, 1920, 3), dtype=_np.uint8)
        img[:, :] = (60, 60, 70)
        for cx, cy, w, h in blobs:
            x0, y0 = int(cx - w / 2), int(cy - h / 2)
            img[y0:y0 + h, x0:x0 + w] = (140, 189, 205)     # BGR 奶油色
        return img

    def _near(got, want, tol=2):
        """质心会有 1~2px 的舍入差（合成图是整数矩形），点按钮完全够用。"""
        return got is not None and abs(got[0] - want[0]) <= tol \
            and abs(got[1] - want[1]) <= tol

    # ① 两台设备各自的真实位置都要认出来（这才是「跨设备」的意义）
    check("模拟器位置 (1832,53) 认出箭头",
          _near(ui.back_arrow_point(_mk([(1832, 53, 58, 44)])), (1832, 53)),
          str(ui.back_arrow_point(_mk([(1832, 53, 58, 44)]))))
    check("手机位置 (1759,132) 认出箭头",
          _near(ui.back_arrow_point(_mk([(1759, 132, 53, 39)])), (1759, 132)),
          str(ui.back_arrow_point(_mk([(1759, 132, 53, 39)]))))
    check("老固定候选 (1832,53) 在手机位置上确实落空（这就是那个 bug）",
          abs(1759 - 1832) > 40 and abs(132 - 53) > 40)

    # ② 保守性：0 个 / 多个 / 形状不对，一律 None（宁可慢，不要瞎点）
    check("没有色块 → None", ui.back_arrow_point(_mk([])) is None)
    check("有两个色块（歧义）→ None",
          ui.back_arrow_point(_mk([(1832, 53, 58, 44), (1759, 132, 53, 39)])) is None)
    check("方块（宽高比 1.0，不像箭头）→ None",
          ui.back_arrow_point(_mk([(1832, 53, 44, 44)])) is None)
    check("太小的噪点 → None", ui.back_arrow_point(_mk([(1832, 53, 12, 9)])) is None)
    check("位置偏左（不在右上角区域）→ None",
          ui.back_arrow_point(_mk([(700, 53, 58, 44)])) is None)
    check("位置偏下（不在顶部区域）→ None",
          ui.back_arrow_point(_mk([(1832, 700, 58, 44)])) is None)
    check("画面为 None → None", ui.back_arrow_point(None) is None)

    # ③ 判据常量自洽
    check("宽高比区间能容下实测的 58/44=1.32 与 53/39=1.36",
          BACK_ARROW_BOX["min_ratio"] <= 1.32 <= BACK_ARROW_BOX["max_ratio"]
          and BACK_ARROW_BOX["min_ratio"] <= 1.36 <= BACK_ARROW_BOX["max_ratio"])

    # ④ 接线：to_home 必须把几何点**排在最前**，且定位不到时回退老候选
    with open(os.path.join(ROOT, "stzb", "ui.py"), encoding="utf-8") as f:
        src = f.read()
    check("to_home 里调了 back_arrow_point", "back_arrow_point(read_png(path))" in src)
    check("几何点插到候选最前（cands.insert(0, arrow)）", "cands.insert(0, arrow)" in src)
    check("_try_closes 加了「关得慢」的二次确认（避免误判没点中）",
          "closechk2" in src)
    check("认不出的界面：画面明显变化就停手（避免关掉后又往新画面点）",
          "画面已变化" in src)


def test_device_job():
    """后端派任务指定设备：serial 必须贯穿「排任务 → 派发 → 执行 → 回执」。

    ★ 事故回顾（2026-09-21）：`/jobs` 页面上只有「执行客户端」下拉，**没有设备**；
      `run_requests` 表也只有 client_id；接口只回 id/slot/only/client_id；
      agent 起 run_daily 只传 `--job-only`。于是「用手机跑还是用模拟器跑」
      在**整条链路上都表达不出来** —— 客户端只能靠 config.device.serial_candidates
      猜设备，而实体机 serial（`340436524100AJ8`，**没有冒号**）天生不在候选表里。
      表现就是「从后端发不起手机任务」：派了也没人跑得起来，或者悄悄跑到了模拟器上。

    这一组钉住四件事：
      ① 数据层有 device_id / serial（含老库迁移）；
      ② pick_job 会**按设备过滤**（不领别的设备的活 —— 在错误设备上跑 = 花错号的资源）；
      ③ 接口/agent/run_daily 三处都把这东西透传下去；
      ④ 回执不把「启动就失败」说成「完成」。
    """
    print("\n[41] 设备任务：后端指定设备跑（serial 全链路）")
    import io
    import stzb.remote_config as _RC

    with io.open(os.path.join(ROOT, "server", "app", "db.py"), encoding="utf-8") as f:
        dbs = f.read()
    check("run_requests 表有 device_id 与 serial 两列",
          "device_id   INTEGER" in dbs and "serial      TEXT" in dbs)
    check("迁移表里也有这两列（老库能自动补上，不用重建）",
          '"device_id": "INTEGER"' in dbs and '"serial": "TEXT"' in dbs)
    check("request_create 接受 device_id / serial",
          "device_id: Optional[int] = None" in dbs
          and "serial: Optional[str] = None" in dbs)
    check("request_list 带出设备名（页面能回显排给哪台）",
          "d.name AS device_name" in dbs)

    # ---------------- pick_job 的行为（这是最容易写错、也最危险的一处）
    class _C:
        def __init__(self, jobs):
            self.jobs = jobs
            self.taken = []

        def list_jobs(self):
            return True, {"jobs": self.jobs}

        def take_job(self, jid):
            self.taken.append(jid)
            return True

    def _j(i, serial=""):
        return {"id": i, "slot": "auto", "only": "", "dry_run": False,
                "note": "", "serial": serial}

    c = _C([_j(1, "127.0.0.1:7555"), _j(2, "340436524100AJ8")])
    got = _RC.pick_job(c, logger=lambda m: None, serial="340436524100AJ8")
    check("锁了手机时跳过模拟器那条、领到手机那条",
          bool(got) and got["id"] == 2 and c.taken == [2],
          "%s / taken=%s" % (got, c.taken))

    c2 = _C([_j(1, "127.0.0.1:7555")])
    got2 = _RC.pick_job(c2, logger=lambda m: None, serial="340436524100AJ8")
    check("队列里只有别的设备的活 → 一条都不领（留给那台设备）",
          got2 is None and c2.taken == [], "%s / taken=%s" % (got2, c2.taken))

    c3 = _C([_j(1, "")])
    got3 = _RC.pick_job(c3, logger=lambda m: None, serial="340436524100AJ8")
    check("没指定设备的通用任务 → 锁了哪台都能领（按本机配置跑）",
          bool(got3) and got3["id"] == 1, str(got3))

    c4 = _C([_j(1, "127.0.0.1:7555")])
    got4 = _RC.pick_job(c4, logger=lambda m: None, serial="")
    check("本机自己没锁设备（老路径）→ 任何任务都能领，行为不变",
          bool(got4) and got4["id"] == 1, str(got4))

    c5 = _C([])
    check("队列空 → None（不抛）",
          _RC.pick_job(c5, logger=lambda m: None, serial="x") is None)

    # ---------------- 接线：三处都要透传
    with io.open(os.path.join(ROOT, "server", "app", "routes_agent.py"),
                 encoding="utf-8") as f:
        ags = f.read()
    check("接口把 serial / device_id 下发给客户端",
          '"serial":' in ags and '"device_id":' in ags)

    with io.open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as f:
        agents = f.read()
    check("agent 会看队列第一条要哪台设备并传 --serial",
          "_peek_job_serial" in agents and '"--serial"' in agents)

    with io.open(os.path.join(ROOT, "run_daily.py"), encoding="utf-8") as f:
        rds = f.read()
    check("run_daily 领任务时带上自己的目标设备（不领错设备的活）",
          "pick_job(client, logger=log, serial=TARGET_SERIAL)" in rds)
    check("任务自带设备而本机没定 → 以任务为准（定时任务路径也能跑对）",
          "jserial" in rds)
    check("任务设备与本机目标冲突 → 直接退出，不冒险跑错设备",
          "不冒跑错设备的风险" in rds)

    # ---------------- 回执：别把「一步没跑」说成「完成」
    check("回执同时看「上传成功」和「本轮跑成功」",
          "run_ok" in rds and "run_ok=False" in rds)
    check("_bail（启动就失败）明确回执为非完成",
          "run_ok=False)   # ← 启动阶段就失败了" in rds)
    check("主流程回执的判据与返回码同源（不会自相矛盾）",
          "run_ok=bool(res) and all(res.values())" in rds)


if __name__ == "__main__":
    print("=" * 62)
    print("  率土之滨自动化 —— 离线自检")
    print("=" * 62)
    test_match()
    test_shuishou()
    test_texing()
    test_state()
    test_merge()
    test_price()
    test_stamp()
    test_neizheng_not_sub()
    test_gongpin()
    test_blacklist()
    test_info_popup()
    test_kw_point()
    test_main_city_tooltip()
    test_shijing()
    test_recruit_free_badge()
    test_run_lock()
    test_anchors()
    test_recruit_half_price()
    test_yanwu()
    test_yanwu_not_home()
    test_emulator()
    test_report()
    test_remote_config()
    test_cloud_upload()
    test_run_mode()
    test_screenshot_retry()
    test_config_tool()
    test_bat_files()
    test_config_autoresolve()
    test_title_page()
    test_home_recruit_missed()
    test_foreground_guard()
    test_cleanup()
    test_panel_ocr_fallback()
    test_junqing_and_recruit()
    test_current_role()
    test_role_dialog()
    test_role_vert_norm()
    test_run_liveness()
    test_device_target()
    test_awake_guard()
    test_frame_fit()
    test_device_job()
    test_home_tabs()
    test_back_arrow()
    print("\n" + "=" * 62)
    print("  通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 62)
    sys.exit(0 if FAIL == 0 else 1)
