# -*- coding: utf-8 -*-
"""六个每日任务的业务流程。

安全铁律：
  * 只点「免费 / 领取 / 征收 / 获取1张」这类白名单按钮；
  * 「购买 / 批量购买 / 加速 / 立即获取 / 20征收 / 退出 / 确定退出」一律不点，
    除非配置显式允许（目前只有「招募半价时用玉符兑虎符」这一处例外）；
  * 每一步都截图留证，出问题能倒查。

坐标体系：1920x1080 横屏。
"""
from __future__ import annotations

import os
import re
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .core import (Device, Point, TextItem, find_text, match_score, norm,
                   ocr_image_scaled, read_png)
from .report import STATUS_ERROR
from .ui import (KW, YW_CLAIM_XY, YW_DLG_CLOSE, YW_SWEEP_ENTRY, Ui)

# --------------------------------------------------------------------------- 常量

# 税收面板：3 个格子的中心 x，按钮行 y
SS_SLOT_X = (427, 948, 1467)
SS_BTN_Y = 880
SS_BTN_Y_MIN = 835          # 只认这个高度以上的「征收」，避免点到格子标题
SS_BTN_Y_MAX = 970          # 按钮行的下沿（再往下是铜钱条）
# 判断「已征收」红章的门槛。实测数据（1920x1080 截图）：
#   斜盖章   : 面积 1036，外接框 141x78
#   付费按钮上的「征收」两个小红字 : 面积 264，外接框 22x30
# 所以「面积 >= 600 且 宽 >= 100」能把两者干净地分开。不要加形态学膨胀——
# 9x9 的 CLOSE 会把付费按钮那几个小红字连成 713px 的大块，直接造成误判。
SS_STAMP_MIN_AREA = 600
SS_STAMP_MIN_W = 100
# 「强征（花虎符）」按钮的颜色指纹（同样实测得来）：
#   槽1「20/强征」: 两段红字各 面积 265 / 宽 22 / 高 30；按钮是白底，白色占比 0.655
#   槽2/3「等待中」: 没有任何红字，白色占比 0.02
# 「有小红字 + 白底」= 付费按钮。它和红章（面积 ≥600 且宽 ≥100）互斥，不会撞车。
SS_PAID_MIN_AREA = 100
SS_PAID_MAX_AREA = 600
SS_PAID_MAX_W = 60
SS_PAID_WHITE_MIN = 0.30

# 市井「宝物商队」第一个格子的免费按钮（兜底）
SJ_FREE_XY = (837, 318)

# 招募
RC_PACK_COL = [  # 卡包列中心 x（从左到右）
    (330, "新势初起"), (455, "新势初起I"), (755, "开国之才"),
    (945, "魏晋名将"), (1030, "赛季名将"),
]
RC_PACK_TARGET = "魏晋名将"
RC_FREE_LABEL_DXY = (340, 70)      # 「免费」标签相对「招募1次」的搜索范围
RC_DRAW_POINTS = [(1431, 507), (729, 910)]   # 侧栏 / 整页两种布局的「招募1次」位置
RC_HUFU_BUY_MAX = 100

# 「免费」绿标签的实测参数：40x32、面积 639，中心在「招募1次」左侧 106px、下方 23px
RC_BADGE_DX = (-150, -50)          # 相对按钮中心：搜索区间的左/右边界
RC_BADGE_DY = 55                   # 上下各展开多少
RC_BADGE_MIN_AREA = 250
RC_BADGE_W = (22, 80)
RC_BADGE_H = (18, 55)

# 「打折」红丝带的实测参数（2026-09-19 补）。
# 它长在「招募1次」按钮的左边缘，是白底红字、两字上下竖排（打 / 折），
# Windows OCR 完全读不出来（整帧 OCR 里根本没有「打折」这两个字），
# 所以只能靠颜色 + 形状认。实测（1920x1080 的招募面板截图）：
#   丝带    box=(784,458)-(824,543)  40x85  面积 2491  高宽比 2.12
#   按钮里的价格红字「100」 16x22   面积 133   高宽比 1.38
# 面积差 18 倍、高宽比也分开，所以「面积 ≥600 且 高 ≥ 1.5×宽」能干净区分。
# 丝带中心相对「招募1次」按钮中心 ≈ (-105, +14)。
RC_DISC_DX = (-210, -40)           # 相对按钮中心的左/右边界
RC_DISC_DY = 80                    # 上下各展开多少
RC_DISC_MIN_AREA = 600
RC_DISC_W = (25, 70)
RC_DISC_H = (55, 130)
RC_DISC_MIN_HW = 1.5

# 招募价格的位置：在「招募1次」按钮白块的**第二行**（按钮文字正下方），
# 不是左右两侧。实测价格行 box=(867,507)-(959,541)，按钮文字 box=(840,471)-(979,502)。
RC_PRICE_DX = 60                   # 相对按钮框左右各放宽多少
RC_PRICE_DY = 85                   # 按钮文字下沿往下找多少
RC_PRICE_SCALES = (2, 3)           # 直读不到价格时的放大倍数（放大整图再 OCR）

# 演武「扫荡奖励」（2026-09-19 实机测出来的）。
# 右下角那块可点区域：宝箱图标 + 文字「扫荡奖励」 + 倒计时。
#
# ⚠️ 为什么入口必须按**颜色**定位、不能用 OCR 的框：
#    那个「扫荡奖励」的 OCR 框会飘 —— 同一块面板连拍 5 帧，框中心 y 从 981 跳到
#    1005 / 1012 / 1036 / 1041（差了 60px），因为 OCR 经常把下面的倒计时并进同一个框。
#    拿框中心去点，正好落在「宝箱」和「文字」之间的空隙上：真机三次全打偏。
#    宝箱的金色块反而**一动不动**：连拍 5 帧都是 (1718,874)-(1799,938)，
#    81x64、面积 2917 —— 点它的中心 (1758,906) 一次就弹出奖励框。
YW_ICON_BOX = (1660, 1860, 840, 980)    # 搜宝箱的窗口 x1,x2,y1,y2（贴着宝箱，别放宽）
YW_ICON_MIN_AREA = 1200
YW_ICON_W = (50, 130)
YW_ICON_H = (40, 100)
# 完全找不到金色块时的兜底（宝箱中心）与 OCR 兜底顺序
YW_SWEEP_ENTRY = [(1758, 906), (1758, 981), (1758, 1016)]
# 「扫荡演武奖励」弹窗布局（1920x1080）：标题 y≈111、副标题 y≈244、
# 奖励格子 y≈430~690、底部通栏按钮条 y≈807~860（深色条实测 (484,807)-(1387,860)）。
YW_CLAIM_Y = (740, 940)            # 「领取」按钮只认这个高度区间
# 弹窗里必然同时出现的 9 种奖励名。用它确认「这确实是扫荡奖励弹窗」——
# 光看「扫荡奖励」四个字不行：演武面板右下角本来也有这四个字。
YW_DIALOG_ITEMS = ("木材", "铁矿", "粮草", "石料", "铜钱",
                   "战法经验", "虎符", "属性阅历", "战法阅历")
YW_DIALOG_MIN = 5
# 右下角「扫荡奖励」文字的区域（只在找不到宝箱时才用它兜底）
YW_ENTRY_MIN_X = 1660
YW_ENTRY_MIN_Y = 850


def _find_sweep_icon(img) -> Optional[Point]:
    """按颜色找演武面板右下角「扫荡奖励」的宝箱，返回可点的中心点。

    见上面 YW_ICON_* 的说明：文字框会飘，宝箱不动。窗口卡在右下角，
    长宽比也卡死 —— 免得把背景里的金色山岩当成宝箱（实测有过一块 195x41 的干扰）。
    """
    if img is None:
        return None
    import cv2
    import numpy as np
    x1, x2, y1, y2 = YW_ICON_BOX
    x2 = min(x2, img.shape[1])
    y2 = min(y2, img.shape[0])
    if x2 - x1 < 40 or y2 - y1 < 40:
        return None
    sub = img[y1:y2, x1:x2]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([15, 70, 130]), np.array([40, 255, 255]))
    n, _, st, _ = cv2.connectedComponentsWithStats(mask)
    best = None
    for i in range(1, n):
        x, y, w, h, a = [int(v) for v in st[i]]
        if a < YW_ICON_MIN_AREA:
            continue
        if not (YW_ICON_W[0] <= w <= YW_ICON_W[1]
                and YW_ICON_H[0] <= h <= YW_ICON_H[1]):
            continue
        if best is None or a > best[4]:
            best = (x, y, w, h, a)
    if best is None:
        return None
    x, y, w, h, a = best
    return (x1 + x + w // 2, y1 + y + h // 2)


def _has_free_badge(img, ref: TextItem) -> bool:
    """ref（「招募1次」按钮）左边有没有那枚绿色的「免费」标签。

    「免费」二字 OCR 读不出来，但颜色骗不了人：那是一块饱和绿的小圆角标签，
    实测 40x32、面积 639（白字挖掉一部分后仍有 600+）。
    招募页上绿色元素不少（货币图标、概率提升角标…），所以形状和位置都要卡死。
    """
    if img is None:
        return False
    import cv2
    import numpy as np
    x1 = max(0, ref.center[0] + RC_BADGE_DX[0])
    x2 = min(img.shape[1], ref.center[0] + RC_BADGE_DX[1])
    y1 = max(0, ref.center[1] - RC_BADGE_DY)
    y2 = min(img.shape[0], ref.center[1] + RC_BADGE_DY)
    if x2 - x1 < 40 or y2 - y1 < 30:
        return False
    sub = img[y1:y2, x1:x2]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([35, 120, 120]), np.array([90, 255, 255]))
    n, _, st, _ = cv2.connectedComponentsWithStats(mask)
    for i in range(1, n):
        area, w, h = int(st[i, 4]), int(st[i, 2]), int(st[i, 3])
        if area >= RC_BADGE_MIN_AREA and RC_BADGE_W[0] <= w <= RC_BADGE_W[1] \
                and RC_BADGE_H[0] <= h <= RC_BADGE_H[1]:
            return True
    return False


def _has_discount_ribbon(img, ref: TextItem) -> bool:
    """「招募1次」按钮左边缘那条红色「打折」竖丝带。

    竖排的「打折」二字 Windows OCR 读不出来（实测整帧 OCR 里没有这两个字），
    但红色丝带的形状极稳定 —— 见 RC_DISC_* 里的实测数据。
    安全含义：这条丝带 = 当前这一抽是半价（100），没有它就可能是原价 200。
    """
    if img is None:
        return False
    import cv2
    import numpy as np
    x1 = max(0, ref.center[0] + RC_DISC_DX[0])
    x2 = min(img.shape[1], ref.center[0] + RC_DISC_DX[1])
    y1 = max(0, ref.center[1] - RC_DISC_DY)
    y2 = min(img.shape[0], ref.center[1] + RC_DISC_DY)
    if x2 - x1 < 60 or y2 - y1 < 60:
        return False
    hsv = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    mask = cv2.bitwise_or(
        cv2.inRange(hsv, np.array([0, 120, 120]), np.array([10, 255, 255])),
        cv2.inRange(hsv, np.array([170, 120, 120]), np.array([180, 255, 255])))
    n, _, st, _ = cv2.connectedComponentsWithStats(mask)
    for i in range(1, n):
        area, w, h = int(st[i, 4]), int(st[i, 2]), int(st[i, 3])
        if area < RC_DISC_MIN_AREA:
            continue
        if not (RC_DISC_W[0] <= w <= RC_DISC_W[1] and RC_DISC_H[0] <= h <= RC_DISC_H[1]):
            continue
        if h >= RC_DISC_MIN_HW * w:
            return True
    return False


def _price_under_btn(items: Sequence[TextItem], btn: TextItem) -> Optional[int]:
    """读「招募1次」按钮白块第二行的价格数字（100 / 200 / 950）。

    价格在按钮文字的**正下方**，不在左右 —— 老代码只往左侧找（_price_left_of），
    永远读不到，于是价格检查形同虚设。这里按按钮框的下沿往下扫。
    """
    bx1, by1, bx2, by2 = btn.box
    nums: List[int] = []
    for it in items:
        if it is btn:
            continue
        if not (bx1 - RC_PRICE_DX <= it.center[0] <= bx2 + RC_PRICE_DX):
            continue
        if not (by2 - 5 <= it.center[1] <= by2 + RC_PRICE_DY):
            continue
        for s in _digits(it.text):
            nums.append(int(s))
    return min(nums) if nums else None


def _read_recruit_price(ui: Ui, items: Sequence[TextItem], btn: TextItem,
                        path: str, tag: str) -> Optional[int]:
    """三重手段读价格：帧内直读 → 放大 2 倍整图 OCR → 放大 3 倍整图 OCR。

    放大用整图（ocr_image_scaled），不要先裁再放 —— Windows 原生 OCR 对小于
    约 480x270 的图会静默返回 0 行，裁出来的小图放大也没用。
    """
    p = _price_under_btn(items, btn)
    if p is not None:
        return p
    for f in RC_PRICE_SCALES:
        try:
            big = ocr_image_scaled(path, factor=f, tmp_path=ui.dev.shot_path("%s_z%d" % (tag, f)))
        except Exception:
            continue
        p = _price_under_btn(big, btn)
        if p is not None:
            return p
    return None


# --------------------------------------------------------------------------- 通用小动作

def _log_task(log: Callable[[str], None], title: str) -> None:
    log("")
    log("=" * 62)
    log(">> 任务：%s" % title)


def _tap_reveal(ui: Ui, back_kws: Sequence[str], max_taps: int = 7,
                tag: str = "reveal") -> bool:
    """抽卡后的翻牌动画：点屏幕中央 / 详情页 ✕，直到回到指定界面。"""
    for i in range(max_taps):
        items, _ = ui.ocr("%s_%d" % (tag, i))
        if ui.has(items, *back_kws):
            return True
        sig = ui.guard(items)
        if sig == "hufu_lack":
            return False
        if ui.has(items, "特性效果", "星级要求", "阵营要求", "配点", "兵种"):
            ui.tap(1831, 59)            # 武将/特性详情页右上 ✕
        else:
            ui.tap(960, 540)            # 翻牌页点中间跳过
        time.sleep(1.8)
    items, _ = ui.ocr(tag + "_end")
    return ui.has(items, *back_kws)


def _dismiss_ok_popup(ui: Ui, tries: int = 3) -> None:
    """点掉「确定 / 确认 / 领取 / 知道了」这类结果弹窗。"""
    for _ in range(tries):
        items, _ = ui.ocr("okpop")
        hit = ui.hit(items, "确定", "确认", "领取", "知道了", "知道了")
        if hit is None:
            return
        # 只点屏幕中部的确认键，避免误触底部主界面
        if 300 < hit.center[1] < 1000:
            ui.tap(*hit.center)
            time.sleep(1.5)
        else:
            return


# --------------------------------------------------------------------------- 1. 贡品礼包

def task_gongpin(ui: Ui, cfg, log) -> bool:
    """活动 → 充值好礼 → 贡品礼包（月卡礼包）：领每日玉符。"""
    _log_task(log, "贡品礼包 / 月卡礼包 —— 每日领玉符")
    if not ui.to_home():
        return False
    if not ui.open_activity():
        log("  × 打不开活动面板")
        return False

    # 切到「充值好礼」页签（页签真实位置在左上角，OCR 偶尔会偏，给多个候选点）
    if not ui.click(KW["充值好礼"], xy=[(395, 156), (506, 153), (465, 208)], tag="gp_tab",
                    label="充值好礼", tries=4,
                    verify=("超值贡品", "贡品礼包", "势力基金"), verify_timeout=8):
        log("  × 没有切到「充值好礼」页签")
        return False

    # 点左侧「贡品礼包」
    if not ui.click(KW["贡品礼包"], xy=[(170, 445), (170, 480), (170, 410)], tag="gp_menu",
                    label="贡品礼包", tries=4,
                    verify=("月卡礼包", "每日领取", "超值贡品"), verify_timeout=8):
        log("  × 没打开「贡品礼包」")
        return False

    time.sleep(1.2)
    items, path = ui.ocr("gp_page")
    log("  · 贡品礼包页面已打开：%s" % path)

    ok = False
    # ① 优先找真正的「领取」按钮（排除「立即获得 / 每日领取」这两个说明标签）
    claim = None
    for it in ui.all_of(items, "领取", "一键领取", "立即领取"):
        t = norm(it.text)
        if t in ("立即获得", "每日领取") or "每日" in t:
            continue
        if any(ch.isdigit() for ch in t):
            continue
        claim = it
        break
    if claim is not None:
        log("  → 发现领取按钮「%s」@%s" % (claim.text, claim.center))
        ui.tap(*claim.center)
        time.sleep(2.0)
        _dismiss_ok_popup(ui)
        ok = True
    else:
        # ② 兜底：点「每日领取」的图标试一次。
        #    图标中心实测就在「每日领取」标签正下方约 126px（1171,730），拿不到
        #    标签就用固定坐标。注意：2026-09-18 实测这一页**没有领取按钮**，
        #    点图标弹出的是「玉符」物品说明浮层（原价购买按钮是 ¥30/续期），
        #    所以必须把浮层识别出来，绝不能当成领取成功。
        label = ui.hit(items, "每日领取")
        pt = ((label.center[0], label.center[1] + 126) if label is not None
              else GONGPIN_DAILY_XY)
        log("  → 试着点「每日领取」图标 %s" % (pt,))
        ui.tap(*pt)
        time.sleep(2.5)
        items2, _ = ui.ocr("gp_after")
        if _got_reward(ui, items2):
            _dismiss_ok_popup(ui)
            ok = True
            log("  ✓ 领取成功")
        elif ui.has(items2, *_TOOLTIP_KWS):
            log("  · 弹出的是「玉符」物品说明浮层，不是领取成功")
            log("  · 这一页没有可点的领取按钮 —— 月卡每日玉符是随月卡自动发放的")
        else:
            log("  · 没有出现领取反馈（说明今天已经领过，或领取入口不在这一层）")

    ui.to_home()
    log("  %s 贡品礼包" % ("✓ 完成" if ok else "△ 本次没有可领的（已正常跳过）"))
    return True      # 找不到可领项不算失败，不能拖垮整轮


# 「玉符」物品说明浮层的特征词。OCR 读到这些就说明点出来的是说明，不是领取成功。
_TOOLTIP_KWS = ("当前拥有", "无法交易", "消耗玉符", "优先消耗", "只能通过充值")


def _got_reward(ui: Ui, items: Sequence[TextItem]) -> bool:
    """判断是否真的领到了东西：出现带数字的「获得」/「恭喜」/「+N」，或有确认弹窗。"""
    # 先排掉物品说明浮层 —— 它里面也有「获得」「拥有数量」这类字，会误判成领取成功
    if ui.has(items, *_TOOLTIP_KWS):
        return False
    for it in items:
        t = norm(it.text)
        if t in ("每日领取", "立即获得"):
            continue
        if any(k in t for k in ("恭喜", "获得", "领取成功")) and any(ch.isdigit() for ch in t):
            return True
    hit = ui.hit(items, "确定", "确认", "知道了")
    if hit is not None and 300 < hit.center[1] < 1000:
        return True
    return False


GONGPIN_DAILY_XY = (1171, 730)


# --------------------------------------------------------------------------- 2. 内政税收

def task_shuishou(ui: Ui, cfg, log) -> bool:
    """内政 → 税收：领金币，每天最多 3 次。只点免费的「征收」，绝不点「20/征收」。"""
    _log_task(log, "内政税收 —— 领取金币(每天3次)")
    max_times = int(cfg.get("shuishou.max_times", 3))
    if not ui.to_home():
        return False
    if not ui.open_neizheng():
        return False
    if not ui.open_sub("税收"):
        return False

    claimed = 0
    for rnd in range(max_times + 1):
        # 面板文字盖在动态插画上，用多帧 OCR 提高识别率
        items, path = ui.ocr_multi("ss_r%d" % rnd, n=3)
        sig = ui.guard(items)
        if sig == "hufu_lack":
            ui.close_hufu_dialog(items)
            break

        # 余额快照（铜钱），用于校验征收是否真的生效
        before = _find_copper(items)
        img = read_png(path)

        pick = _find_free_zz(ui, items, img)
        if pick is None:
            # 第三重手段：放大 2 倍再 OCR 一次（小字号按钮原生分辨率常整条漏读）
            log("  · 本帧没读到可点的「征收」，放大 2 倍再识一次…")
            items_hi = ocr_image_scaled(path, factor=2)
            pick = _find_free_zz(ui, items_hi, img)
            if pick is not None:
                items = items_hi
                log("    · 放大后读到了 %d 行文字" % len(items_hi))
        if pick is False:
            log("  · 只剩要花虎符的「强征」，跳过（绝不点）")
            break
        if pick is None:
            states = {x: _zz_slot_state(items, x, img) for x in SS_SLOT_X}
            log("  · 没有可免费征收的格子了，逐格状态=%s" % (states,))
            break

        log("  → 第%d次征收：点「%s」@%s" % (claimed + 1, pick.text, pick.center))
        # 顺手记录这一格的颜色指纹，用于积累「免费态」样本（今天免费次数已领完，
        # 还没采到过免费按钮的色样，留痕方便以后校准 SS_PAID_* 阈值）
        log("    · 该格颜色指纹：白底占比=%.3f 红块=%s（免费态样本）"
            % (_white_frac(img, pick.center[0]), _red_blobs(img, pick.center[0])[:3]))
        ui.tap(*pick.center)
        time.sleep(2.5)
        _dismiss_ok_popup(ui)
        claimed += 1

        # 用铜钱变化核对是否真的领到了；没涨说明这格早就领过，立刻收手
        items2, _ = ui.ocr_multi("ss_r%d_after" % rnd, n=2)
        if ui.guard(items2) == "hufu_lack":
            log("    ! 出现「虎符不足」→ 刚才那下没领到免费的，收手")
            ui.close_hufu_dialog(items2)
            claimed -= 1
            break
        after = _find_copper(items2)
        if before is None or after is None or after == 0:
            log("    · 读不到铜钱数目，无法核对，按已领处理")
            continue
        log("    · 铜钱 %s → %s" % (before, after))
        if after <= before:
            log("    · 铜钱没涨 → 这一格其实已经领过了，停止（不再空点）")
            claimed -= 1
            break

    log("  ✓ 税收完成，本次征收 %d 次" % claimed)
    ui.to_home()
    return True


def _find_copper(items: Sequence[TextItem]) -> Optional[int]:
    """税收面板底部的铜钱数量（最大的那个 4-5 位数）。"""
    nums = []
    for it in items:
        t = norm(it.text)
        if t.isdigit() and it.center[1] > 950:
            nums.append(int(t))
    return max(nums) if nums else None


def _find_free_zz(ui: Ui, items: Sequence[TextItem], img=None):
    """在税收面板里找「免费征收」按钮。

    返回 TextItem（可点）、None（今天没得领 / 状态不明）、False（只剩要花虎符的）。

    三重手段叠着用（用户要求「一种不行换另一种」）：
      ① OCR 文字：按钮行里读到干净的「征收」（不带数字、不是「强征」）→ 就是它；
      ② 颜色：按钮行里有「已征收」红章（大而宽的红色连通块）→ 这格已领；
        按钮行里有「强征」的小红字 + 白底 → 这格是付费，绝不点；
      ③ 多帧 / 放大 OCR：由调用方补一轮 ocr_multi + ocr_image_scaled 再叫本函数。

    安全红线（2026-09-18 实测踩过）：**任何时候都不再盲点固定坐标**。
    上一版对状态不明的格子盲点 (427,880)，那一格当时是「20/强征」，
    点下去直接弹「虎符不足」——虎符够的话就白花 20 虎符了。
    宁可漏领一次，也不能乱花钱。
    """
    # ---- ① OCR 直接找免费按钮 ----
    for it in items:
        if it.center[1] < SS_BTN_Y_MIN or it.center[1] > SS_BTN_Y_MAX:
            continue                          # 格子标题行的「征收」不是按钮
        if _is_free_zz_txt(norm(it.text)):
            return it

    # ---- ② 逐格判断状态（OCR 关键词 + 红章颜色 + 付费按钮颜色） ----
    states = {x: _zz_slot_state(items, x, img) for x in SS_SLOT_X}
    if any(st == "free" for st in states.values()):
        # OCR 读到了「征收」但没带出按钮框（少见），用该格中心兜底
        x = next(x for x, st in states.items() if st == "free")
        return _FallbackBtn(x, SS_BTN_Y)
    if any(st == "paid" for st in states.values()):
        return False
    return None


# 「征收」在美术字体 + 动态插画干扰下的实测错读形式
_ZZ_FORMS = ("征收", "征丬攵", "氵正收", "氵正丬攵", "征丬収", "伩收", "征収")
# 只有这些才算「这格不用管了」。
# 注意：绝不能把「征丬攵 / 氵正丬攵」放进来——那是格子标题「征收」的常见误读，
# 三个格子永远都有标题，混进来会导致待征的格子全被当成「已领完」而永远不领。
_ZZ_DONE = ("已征收", "征收中", "征收完成", "加速获取", "加速")
# 付费按钮的字样（花虎符）：绝不能点。
# 「弓征」是实测放大 2 倍后「强征」的 OCR 结果（0/弓征）。
_ZZ_PAID = ("强征", "強征", "弓虽征", "弓征", "强制征收", "立即征收")
# 等待/冷却
_ZZ_WAIT = ("等待中", "等待", "冷却", "倒计时")


def _is_zz(t: str) -> bool:
    """判断这段归一化文字里有没有「征收」字样（容忍 OCR 误读）。"""
    if any(f in t for f in _ZZ_FORMS):
        return True
    return match_score(t, "征收") >= 0.62


def _is_free_zz_txt(t: str) -> bool:
    """这段文字是不是「可以直接点」的免费征收按钮。

    必须是征收字样，且不带数字（「20/征收」是付费）、不含「强征」类字样。
    """
    if not _is_zz(t):
        return False
    if any(p in t for p in _ZZ_PAID):
        return False
    return not any(ch.isdigit() for ch in t)


def _has_done_stamp(img, x: int) -> bool:
    """按钮行里有没有「已征收」红章。

    这枚章是斜着盖的（约 -15°），OCR 基本读不出来（实测 4 帧合并都没读到），
    所以只能看颜色：亮红 + 又大又宽的连通块。
    格子标题栏是暗红（V 低）不会误判；付费按钮上那两个小红字宽度只有 22px，
    被 SS_STAMP_MIN_W 挡住，也不会误判。
    """
    return any(a >= SS_STAMP_MIN_AREA and w >= SS_STAMP_MIN_W
               for a, w, _ in _red_blobs(img, x))


def _has_paid_btn(img, x: int) -> bool:
    """按钮行里是不是「强征（花虎符）」按钮 —— 第二重判据，纯看颜色。

    实测「20/强征」的形貌：白色圆角底（按钮行白色占比 0.655），上面两段红色小字
    （面积各约 265px、宽 22px）。而「已征收」章是面积 1000+ 、宽 140+ 的大块，
    「等待中」那一格则完全没有红色（白色占比 0.02）。
    所以判据是：有小红字 + 底色是白的。三者互不混淆。
    """
    if img is None:
        return False
    small_text = any(SS_PAID_MIN_AREA <= a <= SS_PAID_MAX_AREA and w <= SS_PAID_MAX_W
                     for a, w, _ in _red_blobs(img, x))
    if not small_text:
        return False
    return _white_frac(img, x) >= SS_PAID_WHITE_MIN


def _red_blobs(img, x: int) -> List[tuple]:
    """按钮行里的亮红连通块，返回 [(面积, 宽, 高)]，从大到小。"""
    if img is None:
        return []
    y1, y2 = SS_BTN_Y_MIN, SS_BTN_Y_MAX
    x1, x2 = max(0, x - 200), min(img.shape[1], x + 200)
    roi = img[y1:y2, x1:x2]
    if roi.size == 0:
        return []
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 110, 110]), np.array([12, 255, 255])) | \
        cv2.inRange(hsv, np.array([165, 110, 110]), np.array([180, 255, 255]))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    out = [(int(stats[i, 4]), int(stats[i, 2]), int(stats[i, 3]))
           for i in range(1, n)]
    out.sort(reverse=True)
    return out


def _white_frac(img, x: int) -> float:
    """按钮行里近白色像素占比（「强征」按钮是白底，其余格子是暗底）。"""
    if img is None:
        return 0.0
    roi = img[SS_BTN_Y_MIN + 5:SS_BTN_Y_MIN + 85,
              max(0, x - 190):min(img.shape[1], x + 190)]
    if roi.size == 0:
        return 0.0
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 215]), np.array([180, 40, 255]))
    return float(mask.sum()) / 255.0 / mask.size


def _zz_slot_state(items: Sequence[TextItem], x: int, img=None) -> str:
    """判断第 x 个税收格子的状态。

    返回 'done'（已征收）/ 'paid'（只能花虎符强征）/ 'wait'（等待中）/
    'free'（可以免费征收）/ 'unknown'（读不出来）。
    """
    for it in items:
        if abs(it.center[0] - x) > 200:
            continue
        if it.center[1] < SS_BTN_Y_MIN or it.center[1] > SS_BTN_Y_MAX:
            continue                      # 只认按钮行的文字，标题行的「征收」不算数
        t = norm(it.text)
        if any(k in t for k in _ZZ_DONE):
            return "done"
        if any(p in t for p in _ZZ_PAID):
            return "paid"
        if _is_zz(t) and any(ch.isdigit() for ch in t):
            return "paid"                 # 「20/征收」
        if any(k in t for k in _ZZ_WAIT):
            return "wait"
        if _is_free_zz_txt(t):
            return "free"
    # 文字没读出来，靠颜色：红章 → 已领；小红字 + 白底 → 强征
    if _has_done_stamp(img, x):
        return "done"
    if _has_paid_btn(img, x):
        return "paid"
    return "unknown"


class _FallbackBtn:
    """OCR 没读到按钮文字时的合成按钮。"""

    def __init__(self, x: int, y: int):
        self.text = "征收(兜底坐标)"
        self.center = (x, y)


# --------------------------------------------------------------------------- 3. 内政市井

# 市井商品网格：按钮一律是「白底圆角块 + 黑字」，实测 170x48，位置固定成 2 列 3 行。
SJ_GRID_Y = (250, 660)
SJ_BTN_W = (140, 212)
SJ_BTN_H = (36, 62)
# 价格带里绿色（玉符）占比的判据。实测：玉符价 0.053，铜钱价 0.000 —— 分得很开。
SJ_JADE_MIN = 0.02


def _shop_blocks(img) -> List[Tuple[int, int, int, int]]:
    """找出市井里所有商品按钮的白块。

    「免费」和「购买」长得一模一样（都是 170x48 白底黑字），靠外观分不开；
    真正的差别是**文字内容**，而 OCR 偏偏读不出「免费」（白底黑字、字号小，
    实测 1x~3x 全读不出）。所以这里先用颜色把 6 个按钮全找出来，
    再交给 _block_labels 看谁身上有字。
    """
    if img is None:
        return []
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 215]), np.array([180, 45, 255]))
    mask[:SJ_GRID_Y[0], :] = 0
    mask[SJ_GRID_Y[1]:, :] = 0
    n, _, st, _ = cv2.connectedComponentsWithStats(mask)
    out = []
    for i in range(1, n):
        x, y, w, h, a = (int(st[i, 0]), int(st[i, 1]), int(st[i, 2]),
                         int(st[i, 3]), int(st[i, 4]))
        if SJ_BTN_W[0] <= w <= SJ_BTN_W[1] and SJ_BTN_H[0] <= h <= SJ_BTN_H[1] \
                and a >= 0.6 * w * h:
            out.append((x, y, x + w, y + h))
    out.sort(key=lambda b: (b[1], b[0]))
    return out


def _box_center(b: Tuple[int, int, int, int]) -> Tuple[int, int]:
    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)


def _block_labels(items: Sequence[TextItem], b) -> List[TextItem]:
    """落在按钮块上的 OCR 文字。「免费」读不出来 → 返回空列表。"""
    x1, y1, x2, y2 = b
    return [it for it in items
            if x1 - 8 <= it.center[0] <= x2 + 8 and y1 - 8 <= it.center[1] <= y2 + 8]


def _block_has_ink(img, b) -> bool:
    """按钮白块里有没有「字」。

    用来排除误把别处的白色矩形当按钮的情况：真按钮里面一定有黑字
    （实测暗色占比 0.09~0.11），纯白空块约 0。
    """
    if img is None:
        return False
    import cv2
    import numpy as np
    x1, y1, x2, y2 = b
    sub = img[y1:y2, x1:x2]
    if sub.size == 0:
        return False
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    dark = cv2.inRange(hsv, np.array([0, 0, 0]), np.array([180, 255, 110]))
    return float(dark.sum()) / 255.0 / dark.size > 0.03


def _price_band(b) -> Tuple[int, int, int, int]:
    """按钮正上方那条价格带。

    注意：价格图标在按钮的**正上方**，不在「材料名 → 按钮」的水平间隙里。
    老代码只看水平间隙，于是把「赤珠山铁（玉符700）」当成了铜钱价，
    直接点下去、弹出「虎符不足」——虎符要是够就真花了。实测踩过。
    """
    x1, y1, x2, y2 = b
    return (max(0, x1 - 20), max(0, y1 - 85), x2 + 20, max(0, y1 - 3))


def _band_jade_frac(img, b) -> float:
    """价格带里绿色（玉符）像素的占比。"""
    if img is None:
        return 1.0                      # 拿不到图就当成玉符，宁可不买
    import cv2
    import numpy as np
    x1, y1, x2, y2 = _price_band(b)
    sub = img[y1:y2, x1:x2]
    if sub.size == 0:
        return 1.0
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    g = cv2.inRange(hsv, np.array([45, 80, 90]), np.array([95, 255, 255]))
    return float(g.sum()) / 255.0 / g.size


def _band_price(items: Sequence[TextItem], b) -> Optional[str]:
    """价格带里那段可读的价格数字。读不到就返回 None（那就绝不买）。"""
    x1, y1, x2, y2 = _price_band(b)
    for it in items:
        cx, cy = it.center
        if x1 - 30 <= cx <= x2 + 30 and y1 <= cy <= y2:
            digits = "".join(ch for ch in norm(it.text) if ch.isdigit())
            if len(digits) >= 3:
                return digits
    return None


def task_shijing(ui: Ui, cfg, log) -> bool:
    """内政 → 市井：宝物商队领免费材料；能用铜钱买的指定材料就买。

    判定全部围绕「6 个按钮白块」展开：
      · 块上没有 OCR 文字、块内却有字  → 「免费」按钮（OCR 读不出这两个字）
      · 价格带绿色占比 ≥ 0.02          → 玉符价，绝不买
      · 价格带读得到数字且不绿          → 铜钱价，可以买
    三条都对不上就什么都不做，宁可漏买也不花错钱。
    """
    _log_task(log, "内政市井 —— 免费物品 / 铜钱买材料")
    free_item = bool(cfg.get("shijing.free_item", True))
    want: List[str] = list(cfg.get("shijing.buy_materials", []) or [])
    copper_only = bool(cfg.get("shijing.material_pay_copper_only", True))

    if not ui.to_home():
        return False
    if not ui.open_neizheng():
        return False
    if not ui.open_sub("市井"):
        return False

    tab_ok = ui.click(KW["宝物商队"], xy=[(755, 143), (760, 145), (755, 145)], tag="sj_tab",
                      label="宝物商队", tries=4, verify=("购买", "剩余"),
                      verify_timeout=8)
    if not tab_ok:
        items, _ = ui.ocr("sj_tab_chk")
        tab_ok = ui.has(items, "剩余", "购买", "刷新")
    if not tab_ok:
        log("  × 没切到「宝物商队」页签")
        ui.to_home()
        return True                     # 切不到页签不算失败，别拖垮整轮

    got = 0
    bought = 0
    for rnd in range(4):
        items, path = ui.ocr("sj_scan_%d" % rnd)
        img = read_png(path)
        blocks = _shop_blocks(img)
        if len(blocks) < 2:
            log("  · 这一帧没识别到商品按钮（%d 个），不再继续" % len(blocks))
            break

        # ---- ① 先领免费 ----
        if free_item:
            free_blocks = [b for b in blocks
                           if not _block_labels(items, b) and _block_has_ink(img, b)]
            if len(free_blocks) > 1:
                log("  ! 同时出现 %d 个「免费」按钮，本次不动手（拿不准就别点）"
                    % len(free_blocks))
                free_blocks = []
            if free_blocks:
                b = free_blocks[0]
                log("  → 点击「免费」按钮 @%s（块 %s）" % (_box_center(b), b))
                ui.tap(*_box_center(b))
                time.sleep(2.2)
                _dismiss_ok_popup(ui)
                got += 1
                continue                # 领完重新刷一帧，页面会变
            if got == 0:
                log("  · 没有「免费」按钮可点了（已领过 / 本页没有）")

        # ---- ② 再买指定材料 ----
        if want:
            log("  · 检查指定材料：%s" % "、".join(want))
            for name in want:
                for it in ui.all_of(items, name):
                    b = _block_in_row(blocks, it)
                    if b is None:
                        continue
                    if _block_labels(items, b) == []:
                        continue            # 没字 = 免费按钮，不是购买键
                    jade = _band_jade_frac(img, b)
                    price = _band_price(items, b)
                    if jade >= SJ_JADE_MIN:
                        log("    · 「%s」是玉符价（绿占比 %.3f），不买" % (name, jade))
                        continue
                    if price is None:
                        log("    · 「%s」读不到铜钱价格，不买（拿不准就不点）" % name)
                        continue
                    if copper_only and jade >= SJ_JADE_MIN:
                        continue
                    log("    → 购买「%s」@%s（按钮 %s，铜钱 %s）"
                        % (name, it.center, _box_center(b), price))
                    ui.tap(*_box_center(b))
                    time.sleep(2.0)
                    _dismiss_ok_popup(ui)
                    bought += 1
                    time.sleep(1.0)
                    items, path = ui.ocr("sj_buy_%d" % bought)
                    img = read_png(path)
                    blocks = _shop_blocks(img) or blocks
        break

    if free_item:
        log("  %s 免费物品领取：%d 次" % ("✓" if got else "·", got))
    if want:
        log("  %s 指定材料购买：%d 笔" % ("✓" if bought else "·", bought))

    ui.to_home()
    return True


def _block_in_row(blocks, item: TextItem) -> Optional[Tuple[int, int, int, int]]:
    """找「和这条材料名同一行、且在它右边」的按钮块（取水平距离最近的那个）。"""
    best = None
    best_d = 10 ** 9
    for b in blocks:
        if b[0] <= item.box[2]:                      # 必须在文字右边
            continue
        if not (b[1] - 45 <= item.center[1] <= b[3] + 10):   # 同一行
            continue
        d = b[0] - item.box[2]
        if d < best_d:
            best, best_d = b, d
    return best


# --------------------------------------------------------------------------- 4. 内政特性

def task_texing(ui: Ui, cfg, log) -> bool:
    """内政 → 特性：只抽「免费」的那一张。按钮变成「立即获取(100玉符)」就放弃。"""
    _log_task(log, "内政特性 —— 免费获取1张（仅12:00档）")
    if not ui.to_home():
        return False
    if not ui.open_neizheng():
        return False
    if not ui.open_sub("特性"):
        return False

    items, path = ui.ocr_multi("tx_panel", n=4)
    log("  · 特性面板已打开（多帧 OCR，%d 行）：%s" % (len(items), path))

    cand = _pick_free_button(ui, items)

    if cand is None:
        paid = ui.hit(items, "立即获取", "立鼠获取", "立即获得")
        if paid is not None:
            # 免费次数用完时按钮变成付费。但如果倒计时马上就归零（比如 12:00 档
            # 跑得比刷新早几分钟），就等一下再抽，避免白跑一趟。
            left = _parse_free_countdown(items)
            wait_max = int(cfg.get("texing.wait_free_seconds", 0) or 0)
            if left is not None and 0 < left <= wait_max:
                log("  · 免费次数还剩 %s，按配置等待后重试…" % _fmt_secs(left))
                time.sleep(left + 6)
                items, _ = ui.ocr_multi("tx_panel2", n=3)
                cand = _pick_free_button(ui, items)
                if cand is None:
                    log("  · 等待后仍不可免费获取 → 不抽")
                    ui.to_home()
                    return True
            else:
                log("  · 只剩付费的「%s」，今天免费次数已用完 → 不抽" % paid.text)
                ui.to_home()
                return True
        else:
            log("  · 没识别到可点的「获取1张」，面板文字如下：")
            for it in items:
                log("      | %s" % it.text)
            ui.to_home()
            return True

    # 二次确认：按钮左边必须有「免费」标签，否则保守放弃
    free_tag = ui.near(items, "免费", cand, dx=320, dy=110)
    if free_tag is None:
        log("  · 「%s」@%s 左边没看到「免费」标签，保守放弃（避免花玉符）"
            % (cand.text, cand.center))
        ui.to_home()
        return True

    log("  → 点「%s」@%s（免费标签 @%s）" % (cand.text, cand.center, free_tag.center))
    ui.tap(*cand.center)
    time.sleep(3.0)

    back_kws = ("获取1张", "立即获取", "立鼠获取", "剩余特性", "免费次数")
    if _tap_reveal(ui, back_kws, tag="tx_reveal"):
        log("  ✓ 特性免费获取成功，已回到特性面板")
    else:
        log("  △ 翻牌后没回到特性面板（可能抽到的卡片详情没关掉）")

    ui.to_home()
    return True


# --------------------------------------------------------------------------- 5. 内政演武

def _find_sweep_entry(ui: Ui, items: Sequence[TextItem]) -> Optional[TextItem]:
    """在演武面板右下角找「扫荡奖励」那块。

    只在右下角找：弹窗副标题「演武挑战至2-4为止的扫荡奖励」里也有这四个字，
    不限位的话会把它当成入口。
    """
    for it in items:
        if it.center[0] < YW_ENTRY_MIN_X or it.center[1] < YW_ENTRY_MIN_Y:
            continue
        if ui.has([it], *KW["扫荡奖励"]):
            return it
    return None


def _is_sweep_dialog(ui: Ui, items: Sequence[TextItem]) -> bool:
    """是不是「扫荡演武奖励」弹窗。

    判据必须两条一起用：
      ① 标题含「扫荡」——OCR 常把「扫」读成「归」（实测两帧都读成「归荡奖励」，
         但 match_score('归荡奖励','扫荡奖励')=0.67，模糊匹配照样命中）；
      ② 9 种奖励名里至少认出 5 个。演武面板本身一个都没有，分得很干净。
    """
    if not ui.has(items, "扫荡", "归荡"):
        return False
    return sum(1 for k in YW_DIALOG_ITEMS if ui.has(items, k)) >= YW_DIALOG_MIN


def _sweep_countdown(items: Sequence[TextItem]) -> Optional[str]:
    """读「22:22:37后可领取」这行倒计时。可领取时没有倒计时，返回 None。

    只认 y≥700 的「数字:数字」——奖励数字（6800/1070/20/16/12）没有冒号，
    副标题「演武挑战至2-4为止」是连字符，都不会误伤。
    """
    for it in items:
        if it.center[1] < 700:
            continue
        if re.search(r"\d\s*[:：]\s*\d{1,2}", it.text):
            return it.text
    return None


def _parse_clock(s: str) -> Optional[int]:
    """把「22:22:37」解析成秒数。"""
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})\s*[:：]\s*(\d{1,2})", s)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _sweep_claim_button(ui: Ui, items: Sequence[TextItem]) -> Optional[TextItem]:
    """底部通栏那个「领取」按钮。冷却中的时候这行是倒计时，没有「领取」二字。"""
    y1, y2 = YW_CLAIM_Y
    for it in items:
        if not (y1 <= it.center[1] <= y2):
            continue
        if "领取" in it.text and "后" not in it.text:
            return it
    return None


def _close_sweep_dialog(ui: Ui, tries: int = 2) -> None:
    """关掉「扫荡演武奖励」弹窗（右上 ✕ 实测 (1832,52)，和通用 ✕ 同一个点）。"""
    for _ in range(tries):
        items, _ = ui.ocr("yw_close")
        if not _is_sweep_dialog(ui, items):
            return
        ui.tap(*YW_DLG_CLOSE)
        time.sleep(1.5)


def task_yanwu(ui: Ui, cfg, log) -> bool:
    """内政 → 演武 → 右下角「扫荡奖励」：每天领一次。

    这是**纯领取**动作，不花任何货币（奖励是木材/铁矿/粮草/石料/铜钱/战法经验/
    虎符/阅历，全是白给），所以安全阀跟花钱的任务不一样：
      * 冷却中（底部显示「X后可领取」）→ 直接跳过，不是失败；
      * 点完必须看到底部变成倒计时，才算真领到（拿「冷却开始」当成功凭据）。
    """
    _log_task(log, "内政演武 —— 领每日「扫荡奖励」")
    if not cfg.get("yanwu.daily_sweep", True):
        log("  · 配置里关掉了「扫荡奖励」→ 跳过")
        return True
    if not ui.to_home():
        return False
    if not ui.open_neizheng():
        return False
    if not ui.open_sub("演武"):
        return False

    items, path = ui.ocr_multi("yw_panel", n=2)
    log("  · 演武面板已打开（多帧 OCR %d 行）：%s" % (len(items), path))
    log("  · 锚点核对：同步主城队伍=%s 扫荡奖励=%s"
        % (ui.has(items, "同步主城队伍"), ui.has(items, "扫荡奖励", "归荡奖励")))

    if not ui.safe_click_guard("扫荡奖励", KW["扫荡奖励"]):
        return False

    wait_max = int(cfg.get("yanwu.wait_free_seconds", 0) or 0)
    opened = False
    for attempt in (1, 2):
        if attempt == 2:
            items, path = ui.ocr_multi("yw_panel_b", n=2)
        # 按颜色找宝箱（稳）；找不到才退到 OCR 坐标
        pt = _find_sweep_icon(read_png(path))
        if pt is not None:
            log("  → 点「扫荡奖励」宝箱 @%s" % (pt,))
        else:
            entry = _find_sweep_entry(ui, items)
            pt = tuple(entry.center) if entry is not None else YW_SWEEP_ENTRY[attempt - 1]
            log("  · 没找到宝箱金色块，改用 OCR 坐标兜底 @%s" % (pt,))
        ui.tap(*pt)
        time.sleep(2.5)

        items, path = ui.ocr_multi("yw_dialog%d" % attempt, n=2)
        if _is_sweep_dialog(ui, items):
            opened = True
            break
        log("  · 点了 %s 没弹出奖励框，换个坐标再试一次" % (pt,))

    if not opened:
        log("  · 两次都没弹出奖励框，屏幕文字如下：")
        for it in items:
            log("      | %s" % it.text)
        ui.to_home()
        return True

    log("  · 「扫荡演武奖励」弹窗已打开（%d 行）：%s" % (len(items), path))

    cd = _sweep_countdown(items)
    if cd is not None:
        secs = _parse_clock(cd)
        if secs is not None and 0 < secs <= wait_max:
            log("  · 还差 %s 才到可领取，按配置等一等再试（兜住 00:00 档跑得比刷新早）"
                % _fmt_secs(secs))
            _close_sweep_dialog(ui)
            time.sleep(secs + 5)
            items, path = ui.ocr_multi("yw_panel_w", n=2)
            pt = _find_sweep_icon(read_png(path)) or tuple(YW_SWEEP_ENTRY[0])
            ui.tap(*pt)
            time.sleep(2.5)
            items, _ = ui.ocr_multi("yw_dialog_w", n=2)
            cd = _sweep_countdown(items)

    if cd is not None:
        log("  · 扫荡奖励冷却中（%s）→ 今天已经领过了，跳过" % cd)
        _close_sweep_dialog(ui)
        ui.to_home()
        return True

    btn = _sweep_claim_button(ui, items)
    if btn is not None:
        log("  → 点「%s」@%s" % (btn.text, btn.center))
        ui.tap(*btn.center)
    else:
        # 兜底：没有倒计时就是可领取状态，而这里是全屏唯一的通栏按钮。
        # 它只会「领奖励 / 没反应」，不会买东西，所以盲点也安全。
        log("  · 没读到「领取」二字（无倒计时说明可领取），点底部通栏 %s 兜底"
            % (YW_CLAIM_XY,))
        ui.tap(*YW_CLAIM_XY)
    time.sleep(3.0)

    items3, path3 = ui.ocr_multi("yw_after", n=2)
    cd2 = _sweep_countdown(items3)
    if cd2 is not None:
        log("  ✓ 领取成功：底部变成「%s」，冷却已开始" % cd2)
    else:
        log("  △ 点完之后底部没出现冷却倒计时，可能没领到（截图 %s），文字如下：" % path3)
        for it in items3:
            log("      | %s" % it.text)

    # 先关掉奖励框，再清掉可能压在上面的「获得奖励」提示条
    _close_sweep_dialog(ui)
    _dismiss_ok_popup(ui)
    ui.to_home()
    return True


# --------------------------------------------------------------------------- 6. 招募

def _pick_free_button(ui: Ui, items: Sequence[TextItem]) -> Optional[TextItem]:
    """挑出真正可点的免费「获取1张」按钮。

    要排除的是「17:35:15后可免费获取1张」这种倒计时说明、以及「立鼠获取」这种
    付费按钮。注意「获取1张」这个名字本身就带数字「1」，所以不能简单地
    「有数字就跳过」——必须先把按钮名本身抠掉，再看剩下的还有没有数字。
    """
    for it in ui.all_of(items, "获取1张", "获取1 张", "取1张"):
        t = norm(it.text)
        if "后" in t and "可" in t:                        # 倒计时说明
            continue
        if "立即" in t or "立鼠" in t or "立刂" in t:      # 付费按钮
            continue
        rest = t
        for nm in ("获取1张", "获取一张", "获取1 张", "取1张"):
            rest = rest.replace(nm, "")
        if any(ch.isdigit() for ch in rest):                # 剩下的数字 = 价格/倒计时
            continue
        return it
    return None


_COUNTDOWN_RE = None


def _parse_free_countdown(items: Sequence[TextItem]) -> Optional[int]:
    """从「17:17:24后可免费获取1张」里解析出剩余秒数。"""
    global _COUNTDOWN_RE
    if _COUNTDOWN_RE is None:
        import re
        _COUNTDOWN_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{1,2})\s*[:：]\s*(\d{1,2})")
    for it in items:
        t = it.text.replace(" ", "")
        if "免费" not in t or "后" not in t:
            continue
        m = _COUNTDOWN_RE.search(t)
        if not m:
            continue
        h, mi, s = (int(x) for x in m.groups())
        return h * 3600 + mi * 60 + s
    return None


def _fmt_secs(n: int) -> str:
    return "%02d:%02d:%02d" % (n // 3600, (n % 3600) // 60, n % 60)


def task_recruit(ui: Ui, cfg, log) -> bool:
    """招募：先抽免费；再按配置决定要不要抽半价（100 虎符）。

    刷新机制（重要，别改错）：游戏每天 **00:00 和 12:00 各刷新一次免费 + 一次半价**，
    所以一天能免费抽 2 次、半价抽 2 次。本任务挂在 00:00 / 12:00 两个档位，
    每个档位跑进来时 `_recruit_once` 各执行一轮「抽免费 + 抽半价」，
    正好对应这两个刷新点。这里**没有任何「每天只抽 1 次」的计数** ——
    判断依据永远是「按钮此刻是不是免费 / 有没有半价丝带」，按当前状态抽，
    所以每个刷新点都能各抽一轮。
    """
    _log_task(log, "招募 —— 免费 / 半价（00:00/12:00 各刷一次，每天 2 免费 + 2 半价）")
    do_free = bool(cfg.get("recruit.free", True))
    do_half = bool(cfg.get("recruit.half_price", False))
    auto_buy = bool(cfg.get("recruit.auto_buy_hufu", False))
    buy_max = int(cfg.get("recruit.hufu_buy_max", 100))
    price_max = int(cfg.get("recruit.half_price_max_hufu", buy_max))
    log("  配置：免费=%s  半价=%s(上限%d虎符)  虎符自动兑换=%s"
        % (do_free, do_half, price_max, auto_buy))

    if not ui.to_home():
        return False
    if not ui.open_recruit():
        return False

    done_free = False
    if do_free:
        done_free = _recruit_once(ui, log, want_free=True, allow_buy=False,
                                  buy_max=buy_max, tag="rc_free")
    else:
        log("  · 免费招募已在配置里关闭，跳过")

    done_half = False
    if do_half:
        done_half = _recruit_once(ui, log, want_free=False, allow_buy=auto_buy,
                                  buy_max=buy_max, price_max=price_max, tag="rc_half")
    else:
        log("  · 半价招募未开启（config.json → recruit.half_price）")

    ui.to_home()
    log("  %s 招募结束（免费:%s 半价:%s）"
        % ("✓" if (done_free or done_half) else "△", done_free, done_half))
    return True


def _select_pack(ui: Ui, log) -> bool:
    """在招募列表里选中「魏晋名将」卡包，打开右侧的抽取抽屉。"""
    for attempt in range(1, 4):
        items, path = ui.ocr("rc_pack_%d" % attempt)
        if ui.has(items, "招募1次", "招募5次"):
            return True
        hit = ui.hit(items, "免费次数", "半价1次", "后免费", RC_PACK_TARGET)
        if hit is not None:
            target = (hit.center[0], hit.center[1] - 250)     # 卡片主体在标签上方
            log("    → 选中卡包，点 %s" % (target,))
            ui.tap(*target)
        else:
            log("    → 没读到卡包标签，用兜底坐标点「魏晋名将」")
            ui.tap(1680, 850)
        time.sleep(3.0)
    items, _ = ui.ocr("rc_pack_chk")
    return ui.has(items, "招募1次", "招募5次")


def _recruit_once(ui: Ui, log, want_free: bool, allow_buy: bool,
                  buy_max: int, tag: str, price_max: Optional[int] = None) -> bool:
    """执行一次招募。want_free=True 表示这次只想用免费次数。

    price_max 是「愿意为这次抽取付多少虎符」的上限（安全阀），默认取 buy_max。
    """
    if not _select_pack(ui, log):
        log("    × 没打开卡包抽取面板")
        return False

    items, path = ui.ocr(tag + "_panel")
    btn: Optional[TextItem] = None
    for it in ui.all_of(items, "招募1次"):
        btn = it
        break
    if btn is None:
        log("    × 没找到「招募1次」按钮")
        return False

    free_tag = ui.near(items, "免费", btn, dx=RC_FREE_LABEL_DXY[0], dy=RC_FREE_LABEL_DXY[1])
    is_free = free_tag is not None
    if not is_free:
        # 「免费」这两个字是白字压饱和绿底、字号又小，Windows OCR 死活读不出来
        # （实测 1x/1.5x/2x/3x 全图都不出），只能看那枚绿标签的颜色和形状。
        if _has_free_badge(read_png(path), btn):
            log("    · OCR 没读到「免费」二字，但按钮左边有绿色「免费」标签 → 按免费处理")
            is_free = True
        else:
            log("    · 「招募1次」左侧没有免费标签（绿标签也不在）")
    frame = read_png(path)
    ribbon = _has_discount_ribbon(frame, btn)
    if ribbon:
        log("    · 按钮左边缘有红色「打折」丝带（竖排字 OCR 读不出，靠颜色认的）")
    discount = ui.near(items, "打折", btn, dx=460, dy=170)
    # 兜底信号：卡包列表里那一列会写「100（半价1次）」，字比丝带大，OCR 读得到
    offer_txt = any("半价" in norm(it.text) for it in items)
    cap = price_max if price_max is not None else buy_max
    price: Optional[int] = None

    if want_free:
        if not is_free:
            log("    · 「招募1次」没有免费标签（今天免费次数已用完）→ 跳过")
            return False
    else:
        if is_free:
            log("    · 还有免费次数，本次按免费处理")
        else:
            if not allow_buy:
                log("    · 「招募1次」需要花虎符，配置未允许 → 跳过")
                return False
            # 关键安全阀：必须确认是「半价」，否则可能按原价 200 消费
            if discount is None and not ribbon and not offer_txt:
                log("    · 按钮左侧没有「打折」丝带、卡包里也没有「半价」字样"
                    "（半价次数已用完）→ 不抽")
                return False
            # 第二道安全阀：价格必须读出来，且不超过上限。
            # 读不出价格就当作「可能是原价」处理 —— 宁可漏抽，也不能按原价买。
            price = _read_recruit_price(ui, items, btn, path, tag)
            if price is None:
                log("    · 读不出当前价格 → 不抽（宁可漏抽也不按原价买）")
                return False
            if price > cap:
                log("    · 当前价 %d 超过允许上限 %d → 不抽" % (price, cap))
                return False
            log("    · 半价已确认（丝带=%s / 打折字=%s / 半价字样=%s），当前价 %d"
                % (ribbon, discount is not None, offer_txt, price))

    log("    → 点「%s」@%s（%s%s）"
        % (btn.text, btn.center,
           "免费" if is_free else "半价",
           "" if is_free else "，价格=%s" % (price if price is not None else "?")))
    ui.tap(*btn.center)
    time.sleep(3.0)

    # 可能弹出「虎符不足」
    items2, _ = ui.ocr(tag + "_after")
    if ui.has(items2, *KW["虎符不足"]):
        if not allow_buy:
            log("    · 虎符不足，关闭弹窗后跳过")
            ui.close_hufu_dialog(items2)
            return False
        log("    · 虎符不足 → 按配置兑换 %d 虎符" % min(buy_max, 100))
        ok, why = _hufu_exchange_ok(items2, buy_max)
        if not ok:
            log("    × %s" % why)
            ui.close_hufu_dialog(items2)
            return False
        log("    · 兑换弹窗已确认（%s）" % why)
        buy_btn = _find_hufu_buy(ui, items2, buy_max)
        if buy_btn is None:
            log("    × 没找到兑换按钮，直接关闭")
            ui.close_hufu_dialog(items2)
            return False
        ui.tap(*buy_btn)
        time.sleep(3.0)
        items3, _ = ui.ocr(tag + "_buy")
        if ui.has(items3, *KW["虎符不足"]):
            log("    × 兑换后仍显示虎符不足，放弃本次")
            ui.close_hufu_dialog(items3)
            return False
        # 兑换成功后通常会直接继续抽卡
        log("    ✓ 虎符已兑换，继续抽卡")

    back_kws = ("招募1次", "招募5次", "赛季卡包", "典籍", "免费次数", "半价1次", "后免费")
    if _tap_reveal(ui, back_kws, max_taps=9, tag=tag + "_reveal"):
        log("    ✓ 招募完成，已返回招募界面")
    else:
        log("    △ 翻牌后没回到招募界面")
    return True


def _price_left_of(items: Sequence[TextItem], btn: TextItem,
                   dx: int = 380, dy: int = 110) -> Optional[int]:
    """读按钮左侧的价格数字（100 / 200 / 950 …）。"""
    nums: List[int] = []
    for it in items:
        if it is btn:
            continue
        ddx = btn.center[0] - it.center[0]
        ddy = abs(btn.center[1] - it.center[1])
        if not (0 < ddx <= dx and ddy <= dy):
            continue
        for s in _digits(it.text):
            nums.append(int(s))
    return min(nums) if nums else None


def _hufu_exchange_ok(items: Sequence[TextItem], buy_max: int) -> Tuple[bool, str]:
    """确认眼前这个弹窗是「虎符不足 → 用玉符按比例兑换」，不是充值页。

    实测该弹窗里可读到的文字（rc_half_after_010.png）：
        虎符不足 / 100 / 还需要100虎符才能进行本次操作 / ！，兑换比例1玉符=1虎符
        / 批量购买 / 100/购买
    只要出现充值类字样就一律放弃 —— 这条是给「点购买」这个动作兜底的。
    """
    joined = " ".join(norm(it.text) for it in items)
    for bad in ("充值", "¥", "￥", "支付", "元宝", "支付宝", "微信"):
        if bad in joined:
            return False, "弹窗里有「%s」字样，像是充值页 → 不点购买" % bad
    ratio_txt = ""
    for it in items:
        if "兑换" in norm(it.text):
            ratio_txt = norm(it.text)
            break
    if not ratio_txt and "还需要" not in joined:
        return False, "没读到「兑换比例 / 还需要…虎符」字样，不敢确认是兑换弹窗"
    nums = [int(s) for s in _digits(ratio_txt)]
    if len(nums) >= 2 and nums[0] > nums[1]:
        return False, "兑换比例不划算（付 %d 只换到 %d）→ 不点" % (nums[0], nums[1])
    return True, ratio_txt or "还需要…虎符"


def _find_hufu_buy(ui: Ui, items: Sequence[TextItem], buy_max: int) -> Optional[Point]:
    """在「虎符不足」弹窗里找「100 / 购买」按钮，且价格不超过 buy_max。"""
    cands = []
    for it in ui.all_of(items, "购买"):
        t = norm(it.text)
        if "批量" in t:
            continue
        nums = [int(s) for s in _digits(t)]
        if nums and min(nums) > buy_max:
            continue
        cands.append(it)
    if not cands:
        return None
    cands.sort(key=lambda i: i.center[0])
    return cands[0].center


def _digits(s: str) -> List[str]:
    out, cur = [], ""
    for ch in s:
        if ch.isdigit():
            cur += ch
        elif cur:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------- 调度

TASKS: Dict[str, Dict] = {
    "gongpin": {"name": "贡品礼包/月卡礼包(每日玉符)", "fn": task_gongpin,
                "at": ("00:00", "12:00")},
    "shuishou": {"name": "内政税收(金币x3)", "fn": task_shuishou,
                 "at": ("00:00", "12:00")},
    "shijing": {"name": "内政市井(免费物品/材料)", "fn": task_shijing,
                "at": ("00:00", "12:00")},
    "yanwu": {"name": "内政演武(每日扫荡奖励)", "fn": task_yanwu,
              "at": ("00:00", "12:00")},
    "texing": {"name": "内政特性(免费获取1张)", "fn": task_texing,
               "at": ("12:00",)},
    "recruit": {"name": "招募(免费/半价)", "fn": task_recruit,
                "at": ("00:00", "12:00")},
}


def run_all(ui: Ui, cfg, slot: str, only: Optional[Sequence[str]] = None,
            logger: Callable[[str], None] = print, report=None) -> Dict[str, bool]:
    """按档位执行任务。slot 取 '00:00' 或 '12:00'。

    report 非空时（stzb.report.RunReport），会逐个任务记录耗时、日志和这一轮新产生的
    截图，跑完能生成一份带图的 HTML 报告。传 None 就是纯日志模式。
    """
    result: Dict[str, bool] = {}
    enabled = cfg.get("tasks", {}) or {}
    for key, meta in TASKS.items():
        if only and key not in only:
            continue
        name = meta["name"]
        if not only and slot not in meta["at"]:
            logger(">> 跳过 %s（不在 %s 档）" % (name, slot))
            if report:
                report.skip(key, name, "不在 %s 档" % slot)
            continue
        if not enabled.get(key, True):
            logger(">> 跳过 %s（配置里已关闭）" % name)
            if report:
                report.skip(key, name, "配置里已关闭")
            continue

        rec = report.begin(key, name) if report else None
        tlog = rec if rec is not None else logger
        t0 = time.time()
        status = None
        reason = ""
        try:
            ok = bool(meta["fn"](ui, cfg, tlog))
        except Exception as e:                      # 单个任务炸了不能拖垮整轮
            logger("!! 任务 %s 异常：%r" % (name, e))
            ok = False
            status = STATUS_ERROR
            reason = "异常：%r" % (e,)
            try:
                ui.to_home()
            except Exception:
                pass
        result[key] = ok
        if rec is not None:
            report.finish(rec, ok, status=status, reason=reason,
                          seconds=time.time() - t0)
    return result
