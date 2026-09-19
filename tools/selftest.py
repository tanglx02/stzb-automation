# -*- coding: utf-8 -*-
"""离线自检：把实测遇到的 OCR 乱码/界面文字喂进匹配与选按钮逻辑，断言行为正确。

重点是安全规则：
  * 「20/征收」这类带数字的付费按钮绝不能被选中；
  * 特性面板只在「获取1张」且旁边有「免费」时才点；
  * 招募卡包详情页不能被误判成主城。

跑法：python tools/selftest.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb.core import TextItem, find_text, match_score, merge_items, norm  # noqa: E402
from stzb.ui import Ui                                                   # noqa: E402
from stzb.tasks import (_FallbackBtn, _band_jade_frac, _band_price,       # noqa: E402
                        _block_has_ink, _block_in_row, _block_labels,
                        _find_free_zz, _find_hufu_buy, _find_sweep_entry,
                        _find_sweep_icon, _got_reward, _has_done_stamp,
                        _has_discount_ribbon, _has_free_badge,
                        _hufu_exchange_ok, _is_free_zz_txt, _is_sweep_dialog,
                        _parse_clock, _parse_free_countdown,
                        _pick_free_button, _price_left_of, _price_under_btn,
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

    # adb 侧：候选顺序优先，认不出就用任意在线设备；gbk 解码不影响这里的 utf-8 输出
    m = MuMu(manager="x", logger=lambda s: None,
             serial_candidates=["127.0.0.1:7555", "127.0.0.1:16384", "emulator-5554"])
    m.adb_serials = lambda: ["emulator-5554", "127.0.0.1:7555"]
    check("多个在线设备时按候选顺序取（实测 7555 和 emulator-5554 是同一台）",
          m.adb_online() == "127.0.0.1:7555")

    m.adb_serials = lambda: ["192.168.1.9:5555"]
    check("只有陌生设备时也先用着", m.adb_online() == "192.168.1.9:5555")

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
        "logging": {"keep_days": 0},
    }
    f = filter_payload(payload)
    check("只留下白名单段（tasks）", set(f.keys()) == {"tasks"}, str(sorted(f.keys())))
    check("device 段被拦掉", "device" not in f)
    check("emulator 段被拦掉", "emulator" not in f)
    check("cloud 段被拦掉（后端不能给自己下发凭据）", "cloud" not in f)
    check("logging 段被拦掉", "logging" not in f)
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
    """独立运行 / 后端托管 的判定。用户明确要求「不绑后端也能直接跑」，这条必须守住。"""
    import run_daily as rd

    print("\n[25] 运行模式（独立运行 vs 后端托管）")

    logs = []
    lg = logs.append

    def cfg_of(cloud):
        return {"cloud": cloud}

    # 1) 默认（enabled 缺失）就是独立运行 —— 这是最关键的一条
    m = rd.resolve_mode(cfg_of({}), offline=False, log=lg)
    check("cloud 段为空时是独立运行", m == rd.MODE_STANDALONE, m)

    m = rd.resolve_mode(cfg_of({"enabled": False, "base_url": "https://x", "token": "t"}),
                        offline=False, log=lg)
    check("cloud.enabled=false 时是独立运行（哪怕地址令牌都填了）", m == rd.MODE_STANDALONE, m)

    # 2) --offline 优先级最高：即使后端配好了也不连
    m = rd.resolve_mode(cfg_of({"enabled": True, "base_url": "https://x", "token": "t"}),
                        offline=True, log=lg)
    check("--offline 能压住已启用的后端", m == rd.MODE_STANDALONE, m)

    # 3) 配全了才是托管
    m = rd.resolve_mode(cfg_of({"enabled": True, "base_url": "https://x", "token": "t"}),
                        offline=False, log=lg)
    check("enabled=true 且地址令牌齐全 → 后端托管", m == rd.MODE_MANAGED, m)

    # 4) 开了但没填全 → 降级为独立运行，且要告警
    for bad, why in (({"enabled": True, "token": "t"}, "缺 base_url"),
                     ({"enabled": True, "base_url": "https://x"}, "缺 token"),
                     ({"enabled": True, "base_url": "   ", "token": "t"}, "base_url 只有空白"),
                     ({"enabled": True, "base_url": "https://x", "token": ""}, "token 是空串")):
        logs.clear()
        m = rd.resolve_mode(cfg_of(dict(bad)), offline=False, log=lg)
        check("配不全（%s）→ 降级为独立运行并告警" % why,
              m == rd.MODE_STANDALONE and any("不完整" in x for x in logs), m)

    # 5) 说明文字里要写清会不会连后端（用户看日志就能分辨）
    s = rd.describe_mode(cfg_of({}), rd.MODE_STANDALONE)
    check("独立运行的说明写明「不连后端」", "不连后端" in s, s)
    d = rd.describe_mode(cfg_of({"base_url": "https://abc.example"}), rd.MODE_MANAGED)
    check("托管模式的说明里带上了后端地址", "https://abc.example" in d, d)

    # 6) 独立运行时上传必须直接返回，一个字节都不发
    calls = []

    class BoomClient:
        def __getattr__(self, name):
            def _f(*a, **kw):
                calls.append(name)
                raise AssertionError("独立运行模式不该碰后端！调用了 %s" % name)
            return _f

    rc = rd._do_upload({"cloud": {"enabled": False}}, None, {"html": "x.html"},
                       None, None, lg, disabled=True, reason="独立运行模式（不连后端）")
    check("独立运行时 _do_upload 返回 None 且不做任何请求", rc is None and not calls)
    check("独立运行时会打印报告留在本机的位置",
          any("报告只留在本机" in x for x in logs), str(logs[-1:]))

    # client=None（托管但初始化失败）同样安全
    rc2 = rd._do_upload({"cloud": {"enabled": True}}, None, {"html": "x.html"},
                        None, None, lg, disabled=False)
    check("client 为 None 时也不崩、不请求", rc2 is None and not calls)

    # 7) --offline 下的 --upload-last 要明确拒绝，而不是偷偷去连
    import io as _io
    import contextlib
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rd.cmd_upload_last({"cloud": {"enabled": True, "base_url": "https://x",
                                             "token": "t"}}, offline=True)
    check("--offline 时 --upload-last 被拒绝并给出解释",
          code == 1 and "不会连接后端" in buf.getvalue(), buf.getvalue()[:80])

    # 8) 命令行真的有这个开关
    import subprocess
    out = subprocess.run([sys.executable, os.path.join(ROOT, "run_daily.py"), "--help"],
                         capture_output=True, text=True, errors="ignore")
    check("--help 里列出了 --offline", "--offline" in (out.stdout or ""))

    # 9) 默认 config.json 必须是独立运行（拷到别的机器不配任何东西也能跑）
    from stzb.config import load as _load
    cfg = _load()
    check("出厂 config.json 的 cloud.enabled 是 False",
          cfg.get("cloud.enabled") is False,
          str(cfg.get("cloud.enabled")))
    m = rd.resolve_mode(cfg, offline=False, log=lg)
    check("★ 出厂配置下判定为独立运行（开箱即可独立跑）", m == rd.MODE_STANDALONE, m)

    # 10) 本机必备段在默认值里都有兜底（config.json 丢了也能跑）
    for sec in ("device", "emulator", "cloud", "logging", "safety", "tasks"):
        check("DEFAULTS 里有 %s 段兜底" % sec, sec in cfg)

    # 11) --status 也必须尊重 --offline。
    #     这个接线漏过一次：函数里写死了 offline=False，实测才发现 --status --offline
    #     仍然报「后端托管」。所以用子进程走一遍真实命令行，而不是只测 resolve_mode。
    env = dict(os.environ)
    env.update({"STZB_CLOUD_ENABLED": "1",
                "STZB_CLOUD_BASE_URL": "http://127.0.0.1:9",
                "STZB_CLOUD_TOKEN": "t"})
    try:
        out = subprocess.run([sys.executable, os.path.join(ROOT, "run_daily.py"),
                              "--status", "--offline"],
                             capture_output=True, text=True, errors="ignore",
                             env=env, timeout=90)
        txt = (out.stdout or "") + (out.stderr or "")
        check("★ --status --offline 报「独立运行」而不是「后端托管」",
              "独立运行" in txt and "后端托管" not in txt, txt[-260:])
        check("--status --offline 会解释是命令行指定的",
              "命令行给了 --offline" in txt, txt[-260:])
    except subprocess.TimeoutExpired:
        check("--status --offline 应在 90 秒内返回（不该去连后端）", False, "超时")


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

    f_bool = tool.ALL_FIELDS["cloud.enabled"]
    check("布尔项认 true/false/是/否/1/0",
          [tool.coerce(f_bool, s)[1] for s in ("true", "否", "1", "off")]
          == [True, False, True, False])

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
        check("后端子菜单能进能出（选解绑时提示本来就是独立运行）",
              "独立运行" in out and "本来就是独立运行" in out, out[-200:])

        # 「查看全部配置」与「备份与恢复」两个菜单项可达
        out = run_menu(cfg2, "12\n\n13\n1\n\n0\n0\n", os.path.join(tmp2, "bak"))
        check("菜单项「查看全部配置」可达", "cloud.enabled" in out)
        check("菜单项「备份与恢复」可达并真的备份了",
              "已备份到" in out and len(os.listdir(os.path.join(tmp2, "bak"))) >= 1)

        # 输入 q 能安全退出（不该崩）
        out = run_menu(cfg2, "q\n", os.path.join(tmp2, "bak"))
        check("顶层输入 q 能安全退出", "配置已保存在" in out)

        # 输入越界编号不崩
        out = run_menu(cfg2, "99\n0\n", os.path.join(tmp2, "bak"))
        check("输入不存在的编号只是提示，不崩", "没有这个编号" in out)
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
    print("\n" + "=" * 62)
    print("  通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 62)
    sys.exit(0 if FAIL == 0 else 1)
