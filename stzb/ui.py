# -*- coding: utf-8 -*-
"""界面导航层：把「设备操作」抽象成「点击某个按钮、进某个面板」。

设计要点（都是实测踩出来的）：
  1. 内政面板有 ±20px 的「呼吸式」漂移，固定坐标会偏；所以每次点击前**当场 OCR**
     再用同一份结果的坐标去点，保证误差最小。
  2. 点击必须「先点 → 再校验目标界面是否出现 → 没出现就重试」，避免动画没结束
     导致的空点。
  3. 每个 ocr 之前先过一遍弹窗守卫：退出确认一律点「取消」（绝不点「退出」）。
  4. 兜底链：OCR 关键词 → 模板匹配 → 固定坐标。
"""
from __future__ import annotations

import os
import time
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from .core import (GAME_PKG, Device, Point, Templates, TextItem, find_all_text,
                   find_login_button, find_masked, find_text, match_score,
                   merge_items, norm, ocr_image, ocr_image_scaled,
                   ocr_region_scaled, read_png, wait_until)

# --------------------------------------------------------------------------- 锚点坐标（1920x1080）

# 主城地图
HOME_TASK = (60, 105)          # 左上「任务」
HOME_ACTIVITY = (296, 175)     # 顶部「活动」
HOME_RECRUIT = (1807, 1036)    # 右下「招募」
HOME_TAB_NEIZHENG = (412, 942)  # 左下「内政」页签

# 内政面板里的入口（会漂移，仅作兜底）
# 有的入口实测必须点「图标」而不是文字（点文字没反应），这里给文字→图标的偏移
NZ_ENTRY_ICON_OFFSET = {
    # 实测：内政里真正可点的是「名字标签」（那枚斜着的六边形），不是上面的菱形图标。
    # 曾经给税收配了 (0,-65) 想点图标，结果 4 次全打在装饰性菱形上，一次都进不去。
    # 所以这里不再偏移，就用 OCR 读到的标签中心。
}

# 内政面板里的入口（会漂移，仅作兜底；给多个候选点，重试时逐个试）
# 顺序 = 命中概率从高到低：先试名字标签，再试标签上下缘，最后才试菱形图标。
NZ_ENTRY_FALLBACK = {
    "政策": [(540, 390), (540, 350)],
    "特性": [(1650, 441), (1650, 425), (1650, 465), (1645, 412), (1645, 500)],
    "税收": [(1770, 600), (1770, 580), (1770, 620), (1770, 555), (1767, 535)],
    "子弟": [(760, 500), (760, 470)],
    "市井": [(1045, 700), (1030, 684), (1045, 730), (1030, 660)],
    "演武": [(1520, 660), (1520, 625)],
    "政务": [(1285, 735), (1285, 700)],
    "市集": [(545, 785), (545, 750)],
    "悬赏": [(1000, 905), (1000, 870)],
}

# 面板右上角的 ✕。实测（2026-09-19）市井 / 特性 / 招募 / 贡品礼包 / 活动 这几个
# 面板的 ✕ 全部落在同一个点 (1832,53)，红块 51x47 —— 所以它必须排第一个，
# 排在后面的话每次关面板都要先白点两个错的坐标（每个 ~3.5 秒）。
# 税收面板比较特殊，它的 ✕ 在内层框的右上 (1771,144)。
SUB_CLOSE = [(1832, 53), (1771, 144), (1792, 158), (1766, 57)]
NZ_BACK = [(1835, 57), (1872, 57), (1810, 57)]                 # 内政/主界面右上返回箭头（实测在 1835,57）

# 弹窗
BTN_CANCEL = (756, 743)        # 「确定退出率土之滨？」→「取消」
BTN_START_GAME = (960, 876)    # 登录页「开始游戏」
# 游戏**标题页**（冷启动必过的一屏）：整屏一张山水画，底部金字
# 「点击以开始游戏」，**点哪儿都行**。实测中心点 (960,540) 可过。
# 注意它和登录页的「开始游戏」是两回事：标题页在登录页之前，且没有按钮框。
TITLE_TAP = (960, 540)
# 网易统一登录页的「登录」按钮兜底坐标（与 stzb/account.py 的 NN_LOGIN_BTN 一致）。
# 正常靠 core.find_login_button() 精确找；找不到才用这个。
NN_LOGIN_BTN = (959, 683)
# 「导致中途退出的原因是？」问卷里的「继续游戏」——实测 (427,837)。
# 注意这个问卷的按钮布局：**「继续游戏」在左、「提交并退出」在右**，
# 所以兜底坐标必须取左边那个，绝不能用「右下角 = 主按钮」的直觉。
BTN_SURVEY_CONTINUE = (427, 837)
AD_CLOSE = (1849, 222)         # 活动广告弹窗右上 ✕
DOWNLOAD_CLOSE = (1534, 233)   # 「资源下载」弹窗右上 ✕（绝不点「开始下载」）

# 招募
RECRUIT_PACK_XIAOJI = (1680, 850)   # 魏晋名将卡包
RECRUIT_1X_FALLBACK = (1431, 507)   # 侧栏「招募1次」
HUFU_DIALOG_CLOSE = (1346, 341)     # 「虎符不足」弹窗右上 ✕
HUFU_BUY_100 = (1177, 711)          # 「100 / 购买」（兑虎符，需开关允许）

# 市井
SHIJING_FREE_FALLBACK = (837, 318)  # 宝物商队第一个格子的「免费」

# 特性
TEXING_GET_FALLBACK = (1480, 824)   # 「获取1张」

# 演武（2026-09-19 实测）
# 右下角的「扫荡奖励」是一整块可点区域：宝箱图标 center=(1758,906)、
# 文字「扫荡奖励」center=(1759,981)、倒计时 center=(1758,1016)。
# 实测点文字中心 (1758,981) 就能弹出「扫荡演武奖励」框。
YW_SWEEP_ENTRY = [(1758, 981), (1758, 906), (1758, 1016)]
# 「扫荡演武奖励」弹窗最底下那条通栏按钮 center=(960,838)（实测深色条 (484,807)-(1387,860)）。
# 可领取时它是「领取」，冷却中它是「22:22:37后可领取」。
YW_CLAIM_XY = (960, 838)
YW_DLG_CLOSE = (1832, 52)           # 该弹窗右上 ✕（与 SUB_CLOSE[0] 同一个点）

# 贡品礼包
ACTIVITY_TAB_RECHARGE = (465, 208)  # 「充值好礼」页签
GONGPIN_MENU = (170, 445)           # 左侧「贡品礼包」
GONGPIN_DAILY_ICON = (1171, 730)    # 「每日领取 150」图标

# 翻牌动画：点屏幕中央跳过
REVEAL_TAP = (960, 540)

# 关键词别名（美术字体 OCR 常误认）
KW = {
    "招募": ("招募",),
    "活动": ("活动", "沽动"),
    "任务": ("任务",),
    "内政": ("内政", "内攻"),
    "政策": ("政策",),
    "市井": ("市井", "井市", "巿井"),
    "税收": ("税收", "橈收", "稅收", "橈叫攵", "税収"),
    "特性": ("特性", "犄性", "持性"),
    "演武": ("演武",),
    "扫荡奖励": ("扫荡奖励", "归荡奖励"),
    "政务": ("政务",),
    "市集": ("市集",),
    "悬赏": ("悬赏",),
    "子弟": ("子弟",),
    "宝物商队": ("宝物商队", "宝物", "商队"),
    "免费": ("免费",),
    "领取": ("领取", "一键领取", "立即领取", "全部领取"),
    "购买": ("购买", "贝勾买"),
    "招募1次": ("招募1次",),
    "获得1张": ("获取1张", "获取1 张", "获取张"),
    "立即获取": ("立即获取", "立鼠获取", "立即获得"),
    "取消": ("取消",),
    "确定": ("确定", "确认"),
    "虎符不足": ("虎符不足",),
    "退出确认": ("确定退出率土之滨", "确定退出"),
    "名望升级": ("名望升级", "解锁新功能", "获得奖励"),
    "充值好礼": ("充值好礼", "充值好札", "玉充值好札"),
    "贡品礼包": ("贡品礼包", "贡品",),
    "每日领取": ("每日领取",),
    "开始游戏": ("开始游戏", "廾始游戏", "并始游戏", "开始游戒", "亓始游戏"),
    # ★ 标题页（冷启动第一屏）。金底金字，全屏 OCR 读 0 行，必须裁底部条带放大才认得出，
    #   实测读成「过`击以开：始：．戏》」。所以这里的别名要按**逐字误认**穷举，
    #   并且判定时用宽松阈值（0.62 的两段式匹配扛不住这种脏结果）。
    "点击以开始游戏": ("点击以开始游戏", "过击以开始游戏", "点击以开始", "以开始游戏"),
    # ★ 网易统一登录页（冷启动第二屏，在标题页之后、「开始游戏」之前）。
    #   它没有「开始游戏」四个字，只有「登录」和「其他账号登录」——
    #   而这两个词都含「登录」，所以判定必须靠多种弱信号（详见 boot()）。
    "网易登录页": ("其他账号登录", "其他帐号登录", "常用", "网易游戏"),
    # 「点击换区」：实测被读成「龙兴之地征服点击换区」整条，关键词是它的一段后缀，
    # 所以除了整词别名，还要能靠「换区」二字命中（下面 _kw_point 会按占比取中点）。
    "点击换区": ("点击换区", "换区"),
    "选择服务器": ("选择服务器",),
    "已有角色": ("已有角色",),
    # ★ 危险问卷：登录页按 Android 返回键会弹出它，里面有「提交并退出」。
    #   识别到就必须点「继续游戏」退出来，绝不能让自动化顺手把游戏退了。
    "中途退出": ("中途退出", "导致中途退出"),
    "继续游戏": ("继续游戏",),
}

HOME_ANCHORS = ("出征", "计略", "土地", "招募")
NZ_ANCHORS = ("政策", "子弟", "市井", "政务", "演武", "市集", "悬赏", "特性", "税收")


class PopupSignal(Exception):
    """弹窗守卫拦下了危险操作时抛出。"""


class Ui:
    def __init__(self, dev: Device, cfg=None, logger: Callable[[str], None] = print,
                 dry_run: bool = False):
        self.dev = dev
        self.cfg = cfg
        self.log = logger
        self.dry_run = dry_run
        self.never_tap: Sequence[str] = ()
        if cfg is not None:
            self.never_tap = cfg.get("safety.never_tap", []) or []
        self.pkg = (cfg.get("device.package") if cfg is not None else None) or GAME_PKG
        self.shots: List[str] = []
        self._tpl: Optional[Templates] = None
        self._fg_cache: Tuple[float, bool] = (0.0, True)   # (时间, 是否在前台)

    # ------------------------------------------------------------------ 前台把关

    def game_foreground(self, ttl: float = 3.0) -> bool:
        """游戏现在是不是真的在前台（带 TTL 缓存）。

        为什么需要它（2026-09-19 实测撞到）：模拟器起着、**游戏没起来**时，
        OCR 读到的是 MuMu 桌面/启动器，而 boot() 会一路走「认不出的界面」分支
        **盲点右上角 ✕**，直到 300 秒超时 —— 实测空转 7 分 48 秒，什么都没干成。
        加这一条后，boot() 能立刻发现「前台不是游戏」并重新拉起，而不是干点。

        实测依据（50 帧全量采样）：游戏内 `mCurrentFocus` 恒定是
        `com.netease.stzb.netease/com.netease.stzb.Client`；不在游戏时是
        `app.lawnchair/app.lawnchair.Launcher` 之类 —— 包名这一层区分得很干净。

        ⚠️ 注意它**只能**判「在不在前台」，**不能**判「在游戏里的哪一屏」——
        游戏内那一行不随界面切换而变（这也是实测结论）。界面判定仍归画面层。
        """
        now = time.time()
        ts, val = self._fg_cache
        if now - ts < ttl:
            return val
        try:
            val = self.dev.game_foreground(self.pkg)
        except Exception:
            val = True                     # 读不到就放行，别把正常流程拦死
        self._fg_cache = (now, val)
        return val

    # ------------------------------------------------------------------ 基础

    @property
    def tpl(self) -> Templates:
        if self._tpl is None:
            folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "templates")
            self._tpl = Templates(folder)
        return self._tpl

    def ocr(self, tag: str = "ui") -> Tuple[List[TextItem], str]:
        items, path = self.dev.ocr(tag)
        self.shots.append(path)
        return items, path

    # 面板判定时，OCR 总行数少于这个值就认为「整屏没读全」，补一轮放大 OCR。
    # 实测（2026-09-20）：税收面板只读到 1 行（真值 12 行），内政面板正常 11~25 行。
    PANEL_MIN_ITEMS = 4

    def _ocr_scaled(self, path: str, factor: int = 2) -> List[TextItem]:
        """把**已经截好的一张图**放大后再 OCR，用于原图整屏漏读时兜底。

        为什么不重新截屏：调用方手上的坐标就是这一帧算出来的，换一帧会错位。

        实测（2026-09-20 00:29 那轮失败）：税收面板是深色插画底 + 小字，
        1920x1080 原图 OCR 整屏只读出 1 行（连「征收」「等待中」都读不出），
        但同一张图放大 2 倍能读出 12 行。放大后坐标会自动除以倍数还原，
        所以结果可以直接和原图结果合并。
        """
        if not path:
            return []
        tmp = None
        try:
            base = os.path.basename(path).rsplit(".", 1)[0]
            tmp = self.dev.shot_path("%s_x%d" % (base, factor))
        except Exception:
            tmp = None
        try:
            return ocr_image_scaled(path, factor=factor, tmp_path=tmp)
        except Exception:
            return []

    def ocr_multi(self, tag: str = "uim", n: int = 4,
                  interval: float = 0.45,
                  scaled_fallback: bool = False) -> Tuple[List[TextItem], str]:
        """多帧 OCR 取并集。用于盖在动态插画上的面板（特性/税收），单帧容易糊。

        `scaled_fallback=True`：并集仍然少得可怜时，把最后一帧放大 2 倍再认一次
        并合并。**面板开没开的判定必须带这个兜底** —— 否则深色面板整屏漏读会被
        误判成「不在这个面板里」，进而退回主城重进、最后报「进不去」。

        默认 False 是为了不改变其它调用点（任务内部找按钮那些）的耗时和语义。
        """
        groups: List[List[TextItem]] = []
        last = ""
        for i in range(max(1, n)):
            items, path = self.dev.ocr("%s_%d" % (tag, i))
            self.shots.append(path)
            last = path
            groups.append(items)
            if i < n - 1:
                time.sleep(interval)
        merged = merge_items(groups)
        if scaled_fallback and len(merged) < self.PANEL_MIN_ITEMS:
            big = self._ocr_scaled(last)
            if len(big) > len(merged):
                merged = merge_items([merged, big])
        return merged, last

    def tap(self, x: int, y: int, delay: float = 0.7) -> None:
        if self.dry_run:
            self.log("    [dry] 点击 (%d,%d)" % (x, y))
            return
        self.dev.tap(x, y, delay)

    def hit(self, items: Sequence[TextItem], *kws: str) -> Optional[TextItem]:
        return find_text(items, *kws)

    def has(self, items: Sequence[TextItem], *kws: str) -> bool:
        return find_text(items, *kws) is not None

    # ------------------------------------------------------------------ 相对定位小工具

    def all_of(self, items: Sequence[TextItem], *kws: str) -> List[TextItem]:
        return find_all_text(items, *kws)

    def near(self, items: Sequence[TextItem], kw: str, ref: TextItem,
             dx: int = 340, dy: int = 70) -> Optional[TextItem]:
        """在 ref 左侧 dx、上下 dy 范围内找 kw（用于「按钮旁边的『免费』标签」）。"""
        best: Optional[TextItem] = None
        best_d = 10 ** 9
        for it in items:
            if match_score(it.text, kw) < 0.62:
                continue
            ddx = ref.center[0] - it.center[0]
            ddy = abs(ref.center[1] - it.center[1])
            if 0 < ddx <= dx and ddy <= dy and ddx < best_d:
                best, best_d = it, ddx
        return best

    def right_of(self, items: Sequence[TextItem], kw: str, ref: TextItem,
                 dx: int = 900, dy: int = 55) -> Optional[TextItem]:
        """在 ref 右侧 dx、上下 dy 范围内找 kw（用于「物品行右侧的购买按钮」）。"""
        best: Optional[TextItem] = None
        best_d = 10 ** 9
        for it in items:
            if match_score(it.text, kw) < 0.62:
                continue
            ddx = it.center[0] - ref.center[0]
            ddy = abs(it.center[1] - ref.center[1])
            if 0 < ddx <= dx and ddy <= dy and ddx < best_d:
                best, best_d = it, ddx
        return best

    def has_digit(self, it: TextItem) -> bool:
        return any(ch.isdigit() for ch in it.text)

    # ------------------------------------------------------------------ 弹窗守卫

    def guard(self, items: Sequence[TextItem]) -> str:
        """处理危险/干扰弹窗。返回 '' 表示放行，否则返回信号名。

        返回 'exit_confirm' 表示「确定退出」已被点掉；
        返回 'hufu_lack'    表示出现「虎符不足」（调用方决定是否兑换）。
        """
        if self.has(items, *KW["退出确认"]):
            self.log("    ! 出现退出确认弹窗 → 点「取消」（绝不点退出）")
            hit = self.hit(items, *KW["取消"])
            self.tap(*hit.center) if hit else self.tap(*BTN_CANCEL)
            time.sleep(1.5)
            return "exit_confirm"

        if self.has(items, *KW["虎符不足"]):
            self.log("    ! 出现「虎符不足」弹窗")
            return "hufu_lack"

        # ★ 「导致中途退出的原因是？」问卷 —— 登录页按 Android 返回键就会弹出来。
        #   它下面就是「提交并退出」，点错等于把游戏退了、整轮任务全废。
        #   唯一安全的键是「继续游戏」。实测踩过：探测脚本按了返回键，
        #   问卷盖住整屏，后续 8 次探测全看到同一画面、什么都没摸到。
        if self.has(items, *KW["中途退出"]):
            hit = self.hit(items, *KW["继续游戏"])
            if hit is not None:
                self.log("    ! 出现「中途退出」问卷 → 点「继续游戏」（绝不点提交并退出）")
                self.tap(*hit.center)
            else:
                self.log("    ! 出现「中途退出」问卷但找不到「继续游戏」，用固定坐标兜底")
                self.tap(*BTN_SURVEY_CONTINUE)
            time.sleep(2.0)
            return "exit_survey"

        # 名望升级 / 解锁新功能：纯信息弹窗，点「确定」关掉（不是消费确认）
        if self.has(items, *KW["名望升级"]):
            self.log("    · 「名望升级」信息弹窗 → 点「确定」")
            hit = self.hit(items, *KW["确定"])
            self.tap(*hit.center) if hit else self.tap(*BTN_INFO_OK)
            time.sleep(1.5)
            return "info_popup"

        # 广告弹窗：文案里带「来网易游戏中心」（「精彩活动」是活动面板标题，不能当弹窗）
        if self.has(items, "来网易游戏中心"):
            self.log("    · 检测到广告弹窗，点右上关闭")
            self.tap(*AD_CLOSE)
            time.sleep(1.5)
            return "ad"

        # 资源下载提示：只关掉，绝不点「开始下载」
        if self.has(items, "资源下载") and self.has(items, "总包体", "功能说明"):
            self.log("    · 资源下载弹窗 → 点右上 ✕ 关闭（不点开始下载）")
            self.tap(*DOWNLOAD_CLOSE)
            time.sleep(1.5)
            return "download"

        # 登录页
        if self.has(items, *KW["开始游戏"]) and not self.is_home(items):
            self.log("    · 登录页 → 点「开始游戏」")
            self.tap(*BTN_START_GAME)
            time.sleep(15)
            return "login"
        return ""

    # ------------------------------------------------------------------ 启动引导

    def title_page_hit(self, path: str) -> Optional[TextItem]:
        """识别「冷启动标题页」（山水画 + 底部金字「点击以开始游戏」）。

        为什么需要专门的判据（2026-09-19 冷启动实测踩到）：
          这一屏**全屏 OCR 读出 0 行**——金底金字、底图又是同一色系的山水画，
          对比度极低。脚本认不出它 → 一直走「认不出的界面」分支 →
          盲点右上角 ✕ 直到 300 秒启动超时，整轮任务全废。

        为什么用**模板匹配**而不是 OCR（实测对比过）：
          标题那行字是艺术字，区域放大后的 OCR 时好时坏——同一批 17 帧里，
          覆盖度在 0.00~1.00 之间跳，只能认出 8 帧，不能用。
          而模板匹配的区分度极干净：17 帧中标题页**全部 ≥0.85**，
          非标题页（黑屏 / 网易开屏）**≤0.42**。所以主判据用模板，
          阈值取 0.70，两边都留足余量。

        返回命中项（中心用模板定位点），未命中返回 None。
        """
        img = read_png(path)
        if img is None:
            return None
        hit = self.tpl.find(img, "title_page", roi=(0, 860, 1920, 200),
                            threshold=0.70)
        if hit is None:
            return None
        cx, cy, score = hit
        self.log("    · 识别到标题页「点击以开始游戏」（模板命中 %.0f%%）" % (score * 100))
        return TextItem("点击以开始游戏", (cx - 10, cy - 10, cx + 10, cy + 10), (cx, cy), score)

    def is_netease_login_page(self, items: Sequence[TextItem]) -> bool:
        """是不是**网易统一登录页**（冷启动第二屏，在标题页之后）。

        为什么不能只看「登录」两个字：界面上「自动登录 / 上次登录 /
        其他账号登录」全含「登录」，模糊匹配必然误判。
        改用**多信号投票**（这也和 account.py 里 _tap_switch_account 的判据一致）：
          · 有「其他账号登录」 → 铁证
          · 有脱敏账号（159****4508） → 铁证
          · 有「常用」页签 + 网易 logo → 够用
        """
        texts_ = [norm(it.text) for it in items]
        if any(("其他账号登录" in t or "其他帐号登录" in t) for t in texts_):
            return True
        if find_masked(items) is not None:
            return True
        if any(t == "常用" for t in texts_) and any("网易" in t for t in texts_):
            return True
        return False

    def leave_netease_login_page(self, items: Sequence[TextItem]) -> bool:
        """在网易登录页上点「登录」，进入游戏。返回 True 表示已发出点击。

        安全约束（与 account.py 完全一致，绝不能违反）：
          · 只点 _find_login_button() 找到的那个精确「登录」，
            **绝不点「其他账号登录」**（那会走去密码登录流程，脚本没有密码，必然卡死）。
          · 登录页上**永不发 Android BACK**——会弹「中途退出」问卷，
            里面有「提交并退出」，点错等于把游戏退了。
        """
        hit = find_login_button(items)
        if hit is not None:
            self.log("    · 网易登录页 → 点「登录」@%s" % (hit.center,))
            self.tap(*hit.center)
            time.sleep(4.0)
            return True
        self.log("    ! 网易登录页但没找到精确「登录」按钮 → 用固定坐标兜底")
        self.tap(*NN_LOGIN_BTN)
        time.sleep(4.0)
        return True

    def boot(self, timeout: float = 300.0) -> bool:
        """把游戏带到主城：处理登录页、资源下载、公告、退出确认等。

        如果发现已经停在游戏内的某个面板（比如上次跑一半留下的税收面板），
        就调用 to_home() 先退出来，而不是干等。
        """
        end = time.time() + timeout
        rounds = 0
        not_fg = 0                       # 连续「前台不是游戏」的轮数
        while time.time() < end:
            rounds += 1
            items, path = self.ocr("boot_%02d" % rounds)
            if self.is_home(items):
                self.log("  ✓ 已进入主城")
                return True
            # ★ 前台把关（2026-09-19 实测撞到才加的）：
            #   模拟器起着、**游戏没起来**时，OCR 读到的是 MuMu 桌面/启动器，
            #   而下面那些分支会一路走「认不出的界面」→ 盲点右上角 ✕，
            #   直到 300 秒超时 —— 实测整整空转 7 分 48 秒，一个任务都没跑成。
            #   先问一句「前台到底是不是游戏」比对着别人的界面瞎点强得多。
            if not self.game_foreground(ttl=0.0):
                not_fg += 1
                self.log("    ! 前台不是游戏（%s）→ 重新拉起 %s"
                         % (self.dev.foreground_pkg() or "读不到", self.pkg))
                if not self.dry_run:
                    try:
                        self.dev.launch(self.pkg)
                    except Exception as e:
                        self.log("    ! 拉起失败：%r" % (e,))
                time.sleep(6)
                # 连续 3 轮都不在游戏前台 → 说明真的拉不起来，早点失败比干耗好
                if not_fg >= 3:
                    self.log("  !! 连续 %d 轮游戏都不在前台，放弃启动" % not_fg)
                    return False
                continue
            not_fg = 0
            # ★ 标题页（冷启动第一屏）：山水画 + 底部金字「点击以开始游戏」。
            #   它**全屏 OCR 读出 0 行**，所以只能在「几乎没有文字」时，
            #   退一步做区域放大 OCR 才认得出。
            #   实测后果：认不出 → 一直走「认不出的界面」分支 → 干等到 300 秒启动超时，
            #   整轮任务全废（2026-09-19 冷启动实测踩到）。
            if len(items) <= 2:
                tp = self.title_page_hit(path)
                if tp is not None:
                    self.log("    · 标题页 → 点屏幕中央进游戏")
                    self.tap(*TITLE_TAP)
                    time.sleep(10)          # 这一下之后要加载登录页/资源，给足时间
                    continue
            sig = self.guard(items)
            if sig in ("exit_confirm", "download", "ad", "login", "info_popup"):
                continue
            if sig == "hufu_lack":
                self.close_hufu_dialog(items)
                continue
            # ★ 网易统一登录页（冷启动第二屏）。必须在「开始游戏」判定之前处理：
            #   这一屏上没有「开始游戏」，只有「登录」，而「登录」被
            #   「自动登录/上次登录/其他账号登录」反复污染，所以走多信号判定。
            #   实测后果（2026-09-19）：认不出 → 干等到 300 秒启动超时，整轮全废。
            if self.is_netease_login_page(items):
                self.leave_netease_login_page(items)
                continue
            # 其它弹窗：右侧的「取消/跳过/关闭」优先
            for kw in ("取消", "跳过", "关闭"):
                hit = self.hit(items, kw)
                if hit is None or hit.center[0] <= 1100:
                    continue
                # 「跳过战斗动画」是演武面板上的复选框，不是关面板的键。
                # 实测被它连点 5 次：白花 20 秒，还把复选框来回切。
                if "动画" in hit.text:
                    continue
                self.log("    → 关弹窗：点「%s」" % hit.text)
                self.tap(*hit.center)
                break
            else:
                # 停在游戏内的某个面板里 → 主动退出去
                if self._screen_anchors(items) or self.is_neizheng(items) \
                        or self._in_panel("税收", items) or self._in_panel("市井", items) \
                        or self._in_panel("演武", items) or self._in_panel("特性", items):
                    self.log("    · 当前停在游戏内面板，尝试退回主城")
                    self.to_home(max_rounds=8)
                elif self.close_info_popup(items):
                    continue
                elif rounds >= 8:
                    # 认不出的界面（政务/市集/悬赏…）或者残留浮层：等太久就主动出击，
                    # 别干等到 300 秒启动超时（实测被「政务」面板卡死过一整轮）。
                    self.log("    · 等了 %d 轮仍认不出界面 → 强制清屏退回主城" % rounds)
                    self.to_home(max_rounds=5)
                else:
                    self.log("    等待中…（识别到 %d 行文字）" % len(items))
                    time.sleep(4)
        self.log("  !! 启动超时，最后截图：%s" % path)
        return False

    def safe_click_guard(self, text: str, kws: Sequence[str]) -> bool:
        """确认要点的文字不在 never_tap 黑名单里。"""
        t = norm(text)
        for kw in kws:
            for bad in self.never_tap:
                b = norm(bad)
                if b and (b in norm(kw) or norm(kw) == b):
                    self.log("    × 「%s」命中安全黑名单「%s」，放弃点击" % (text, bad))
                    return False
        return True

    def close_info_popup(self, items: Sequence[TextItem]) -> bool:
        """关掉「居中一个确定键」的纯信息弹窗（升级公告、新功能解锁等）。

        安全阀：只要屏幕上出现任何消费/充值/兑换字样，就绝不动手——
        宁可让调用方超时，也不能误点花钱的确定键。
        """
        danger = ("购买", "充值", "支付", "花费", "虎符", "玉符", "元宝",
                  "续期", "¥", "￥", "确认支付", "立即购买")
        for it in items:
            if any(d in norm(it.text) for d in danger):
                return False
        for kw in ("确定", "确认", "知道了"):
            hit = self.hit(items, kw)
            if hit is None:
                continue
            x, y = hit.center
            if 450 <= x <= 1450 and 380 <= y <= 990:     # 居中的大按钮才算弹窗确认键
                self.log("    → 关信息弹窗：点「%s」@%s" % (hit.text, hit.center))
                self.tap(x, y)
                time.sleep(1.5)
                return True
        return False

    # ------------------------------------------------------------------ 点击 + 校验 + 重试

    @staticmethod
    def _kw_point(hit: TextItem, kws: Sequence[str]) -> Point:
        """取命中关键词自己的中心点。

        OCR 会把相邻的两个标签连读成一个框。实测主城顶栏被读成「活动36小时」
        （框 273→441），框中心 (368,175) 正好落在隔壁那个 36 小时 buff 图标上，
        点下去开的是「初出茅庐」buff 详情页 —— 面板一盖，后面几次全打在遮罩上，
        整个贡品礼包任务就废了。

        规则：若命中关键词只是框内的一段前缀，就按字符长度占比，取它那一段的中点。
        """
        x1, y1, x2, y2 = hit.box
        cy = (y1 + y2) // 2
        t = norm(hit.text)
        for kw in kws:
            k = norm(kw)
            if not k or t == k or not t.startswith(k) or len(t) <= len(k):
                continue
            return (int(x1 + (x2 - x1) * (len(k) / len(t)) / 2), cy)
        return hit.center

    def click(self, kws: Sequence[str], xy=None, tag: str = "click",
              tries: int = 4, verify: Optional[Sequence[str]] = None,
              verify_timeout: float = 6.0, label: str = "",
              wait_before: float = 0.0, verify_fn=None) -> bool:
        """点击关键词对应的按钮，带「多候选点 + 校验 + 重试」的容错链。

        候选点顺序：
          1) OCR 命中文字 + 该入口的图标偏移（有的按钮点文字没反应）
          2) OCR 命中文字本身
          3) 命中文字上方/下方 40px（面板呼吸式漂移时的容错）
          4) 调用方给的固定兜底坐标列表
        每次点击后用 verify 校验，没生效就换下一个候选点。

        verify_fn：比 verify 更准的校验函数（收 items 返回 bool）。
        内政入口一律用它——因为 verify 是「命中任一关键词就算过」，而
        「征收」这种词在内政主界面上也有（「立即征收1/3」），会导致第一次点击
        就误判成功、剩下的候选坐标根本没机会试。
        """
        name = label or "/".join(kws)
        pts: List[Point] = []
        if xy is not None:
            if isinstance(xy[0], (list, tuple)):
                pts = [tuple(p) for p in xy]        # type: ignore[arg-type]
            else:
                pts = [tuple(xy)]                   # type: ignore[arg-type]
        off = NZ_ENTRY_ICON_OFFSET.get(name)
        tried: set = set()
        prev_had_hit = False

        for attempt in range(1, tries + 1):
            if wait_before:
                time.sleep(wait_before)
            items, path = self.ocr("%s_%s_%d" % (tag, name[:6], attempt))
            sig = self.guard(items)
            if sig == "exit_confirm":
                items, path = self.ocr("%s_%s_%d_re" % (tag, name[:6], attempt))

            cands: List[Point] = []
            hit = self.hit(items, *kws)
            # 上一次还看得见目标、这次却看不见了 —— 只有在「能明确认出当前停在
            # 别的面板上」时才当成误开。内政界面上的入口名 OCR 本来就会时有时无，
            # 光凭 hit is None 就按 ✕ 会误伤：内政界面的 ✕ 位就是返回键，
            # 一点就把内政退掉了，后面所有候选坐标全打在主城上、必然全失败。
            if (prev_had_hit and hit is None and attempt > 1
                    and not self.is_home(items)
                    and not self._is_neizheng_screen(items)
                    and self._screen_anchors(items)):
                self.log("    ! 「%s」从屏幕上消失，且认得出停在了别的面板 → 先退出来"
                         % name)
                for cp in SUB_CLOSE[:3]:
                    self.tap(*cp, delay=0.0)
                    time.sleep(1.2)
                    items, path = self.ocr("%s_%s_%d_fix" % (tag, name[:6], attempt))
                    if self.is_home(items) or self.hit(items, *kws) is not None:
                        break
                hit = self.hit(items, *kws)
            prev_had_hit = hit is not None
            if hit is not None:
                bx, by = self._kw_point(hit, kws)
                if off:
                    cands.append((bx + off[0], by + off[1]))
                cands.append((bx, by))
                cands.append((bx, by - 40))
                cands.append((bx, by + 40))
            cands.extend(pts)

            if not cands:
                self.log("    × 第%d次既没识别到「%s」也没有兜底坐标" % (attempt, name))
                time.sleep(1.5)
                continue

            pick = None
            for c in cands:
                key = (int(round(c[0] / 12)), int(round(c[1] / 12)))
                if key not in tried:
                    pick, _ = c, tried.add(key)
                    break
            if pick is None:                        # 都试过了，从头再来一遍
                pick = cands[(attempt - 1) % len(cands)]

            if not self.safe_click_guard(name, kws):
                return False
            self.log("    → 点击「%s」→%s" % (hit.text if hit else name, tuple(pick)))
            self.tap(*pick)

            if verify is None and verify_fn is None:
                return True
            if self.wait_for_cond(verify, verify_fn, timeout=verify_timeout):
                return True
            self.log("    ↻ 第%d次点击「%s」未生效，换下一个候选点…" % (attempt, name))
            time.sleep(1.0)

        self.log("    × 「%s」%d 次都没点成，跳过" % (name, tries))
        return False

    def wait_for_cond(self, kws: Optional[Sequence[str]], fn,
                      timeout: float = 12.0, interval: float = 1.2,
                      tag: str = "waitcond") -> bool:
        """轮询等待「关键词出现」或「fn(items) 成立」，期间清掉退出确认弹窗。

        传了 fn 就用放大兜底：fn 是「面板开没开」这类判定，而深色面板原图
        OCR 会整屏漏读，漏读会让校验误判成「没生效」→ 把剩下的候选坐标全白点
        一遍。实测 2026-09-20：税收入口因此白花 45 秒，而且那些多余点击会落到
        **已经打开的面板上**（税格旁边就是付费的「20/征收」），有误触风险。
        """
        end = time.time() + timeout
        n = 0
        while time.time() < end:
            n += 1
            items, _ = self.ocr_multi("%s_%d" % (tag, n), n=1,
                                      scaled_fallback=fn is not None)
            if self.guard(items) == "hufu_lack":
                return False
            if fn is not None and fn(items):
                return True
            if kws and self.has(items, *kws):
                return True
            time.sleep(interval)
        return False

    def wait_for(self, kws: Sequence[str], timeout: float = 12.0,
                 interval: float = 1.2, tag: str = "wait") -> bool:
        """轮询等待某关键词出现，期间自动清掉退出确认弹窗。"""
        end = time.time() + timeout
        n = 0
        while time.time() < end:
            n += 1
            items, path = self.ocr("%s_%s_%d" % (tag, kws[0][:5], n))
            sig = self.guard(items)
            if sig == "hufu_lack":
                return False
            if self.has(items, *kws):
                return True
            time.sleep(interval)
        return False

    def wait_any(self, kw_groups: Sequence[Sequence[str]], timeout: float = 12.0,
                 interval: float = 1.2, tag: str = "waitany") -> Optional[str]:
        """等待任一关键词组出现，返回命中的组内第一个关键词，超时返回 None。"""
        end = time.time() + timeout
        n = 0
        while time.time() < end:
            n += 1
            items, path = self.ocr("%s_%d" % (tag, n))
            sig = self.guard(items)
            if sig == "hufu_lack":
                return "虎符不足"
            for grp in kw_groups:
                if self.has(items, *grp):
                    return grp[0]
            time.sleep(interval)
        return None

    # ------------------------------------------------------------------ 状态判定

    def _short_has(self, items: Sequence[TextItem], kw: str,
                   maxlen: int = 10) -> bool:
        """只在**短标签**里匹配关键词（长度上限 maxlen 个字符）。

        为什么要长度限制：主城顶栏是「势力值21」这种短标签，但活动面板的
        说明长句「…每日登录和提升势力值可获得势力积分…」里也有「势力值」。
        不加限制的话，子串匹配会给那句长文本 0.909 分，把活动面板误判成主城。
        """
        for it in items:
            if match_score(it.text, kw) >= 0.62 and len(norm(it.text)) <= maxlen:
                return True
        return False

    def is_home(self, items: Sequence[TextItem]) -> bool:
        """主城地图：右下有「招募」按钮，同时能看到「出征/计略/土地/势力值」。

        注意不能只用「招募」判断——招募卡包详情页的「招募1次」「招募28次内必赠5星」
        都含「招募」，会被误判成主城（实测踩过）。
        """
        if self.has(items, "招募1次", "招募5次", "心愿积分", "必获赠", "必赠"):
            return False
        # 各种子面板也常顶着「Lv.x土地」（演武面板就有），而顶部公告栏会滚出
        # 「恭喜xx招募到5星武将」——「土地 + 招募」一凑，is_home 就误判成主城。
        # 实测后果：进演武后立刻被判成主城 → open_sub("演武") 以为没进去 →
        # 退回主城重进一次，白花 35 秒。所以子面板的特征词一律先排除。
        if self.has(items, "同步主城队伍", "守军难度", "跳过战斗动画",   # 演武
                    "宝物商队", "剩余特性", "获取1张",                  # 市井 / 特性
                    "已征收", "征收中", "加速获取"):                    # 税收
            return False
        # ★ 右下角「招募」按钮**偶发漏读**（美术字 + 底图噪声）。
        #   漏读时旧代码直接判「不是主城」→ open_neizheng() 以为还在外面 →
        #   反复退主城、重进，最后报「打不开内政面板」。
        #   实测（2026-09-19）：市井任务就这么失败过一次，而同一轮的演武/特性
        #   都成功进去了 —— 是概率性问题，不是逻辑错。
        #   兜底：顶栏那个**短的**「势力值」标签也是主城独有（子面板顶栏没有它），
        #   拿它当第二判据。必须用 _short_has 排除长句里的同名词。
        strong = self.has(items, *KW["招募"]) or self._short_has(items, "势力值")
        if not strong:
            return False
        return any(self.has(items, k) for k in ("出征", "计略", "土地", "势力值"))

    def is_neizheng(self, items: Sequence[TextItem]) -> bool:
        """是不是「内政」界面。

        必须排除主城：主城上点一下资源数字会弹出「铜钱」说明浮层，里面写着
        「内政税收 · 免费征收」「前往市井」——一下子凑齐 2 个锚点，
        把主城判成内政界面。实测踩过：open_neizheng 因此直接返回 True，
        一个点击都没发出去，后续所有操作全部错位（点开的是「任务」面板）。
        """
        if self.is_home(items):
            return False
        cnt = sum(1 for k in NZ_ANCHORS if self.has(items, *KW.get(k, (k,))))
        return cnt >= 2

    def state(self, tag: str = "state") -> str:
        items, _ = self.ocr(tag)
        if self.has(items, *KW["退出确认"]):
            return "exit_dialog"
        if self.has(items, *KW["虎符不足"]):
            return "hufu_lack"
        if self.is_home(items):
            return "home"
        if self.is_neizheng(items):
            return "neizheng"
        if self.has(items, *KW["招募1次"]):
            return "recruit_pack"
        if self.has(items, *KW["充值好礼"]) or self.has(items, "超值贡品"):
            return "activity"
        if self.has(items, *KW["每日领取"]) or self.has(items, "月卡礼包"):
            return "gongpin"
        return "unknown"

    # ------------------------------------------------------------------ 回主城

    def _screen_anchors(self, items: Sequence[TextItem]) -> Sequence[str]:
        """判断当前处于哪个面板，返回该面板的特征词（用于确认「关掉了」）。

        这里的词必须是**只有该面板才会出现**的。踩过的坑：市井以前用
        「荣誉」当锚点，可主城顶栏一直挂着「荣誉 30/30」，于是关面板的循环
        永远等不到锚点消失，只能把 5 个候选坐标全点一遍 —— 每个面板白花 30 秒。
        """
        if self.is_neizheng(items):
            return tuple(NZ_ANCHORS)
        if self.has(items, "宝物商队"):
            return ("宝物商队",)
        if self.has(items, "剩余") and self.has(items, "刷新"):
            return ("剩余", "刷新")
        if self.has(items, "已征收", "征收中", "加速获取"):
            return ("已征收", "征收中")
        if self.has(items, "剩余特性", "获取1张", "立即获取", "免费次数"):
            return ("剩余特性", "获取1张", "立即获取", "免费次数")
        # 演武面板：注意别用「扫荡奖励」当唯一锚点 —— 它也出现在
        # 「扫荡演武奖励」弹窗里，会让关面板的循环以为还没关掉。
        if self.has(items, "同步主城队伍", "守军难度", "跳过战斗动画"):
            return ("同步主城队伍", "守军难度", "跳过战斗动画")
        if self.has(items, "赛季卡包", "典籍", "典藏", "新势初起"):
            return ("赛季卡包", "典籍", "典藏", "新势初起")
        if self.has(items, "招募1次", "招募5次", "心愿积分", "必获赠", "额外赠送"):
            return ("招募1次", "招募5次", "心愿积分", "额外赠送")
        if self.has(items, "月卡礼包", "超值贡品", "每日领取"):
            return ("月卡礼包", "超值贡品", "每日领取")
        return ()

    def _try_closes(self, anchors: Sequence[str], cands: Sequence[Point],
                    per_try: float = 1.8) -> bool:
        """在候选坐标里挨个点，直到特征词消失（说明面板关了）。"""
        for c in cands:
            self.tap(*c, delay=0.0)
            time.sleep(per_try)
            items, _ = self.ocr("closechk")
            sig = self.guard(items)
            if sig == "hufu_lack":
                self.close_hufu_dialog(items)
                return True
            if self.is_home(items):
                return True                      # 已经回到主城就别再点了
            if not self.has(items, *anchors):
                return True
        return False

    def to_home(self, max_rounds: int = 14) -> bool:
        """无论当前在哪，想办法退回主城地图。"""
        for i in range(max_rounds):
            items, path = self.ocr("home_%02d" % i)
            sig = self.guard(items)
            if sig == "hufu_lack":
                self.log("    · 虎符不足弹窗 → 关闭")
                self.close_hufu_dialog(items)
                continue
            if self.is_home(items):
                self.log("  已在主城。")
                return True
            if sig in ("exit_confirm", "download", "ad", "login", "info_popup"):
                continue
            for kw in ("取消", "跳过", "关闭"):
                hit = self.hit(items, kw)
                if hit is None or hit.center[0] <= 1100:
                    continue
                # 「跳过战斗动画」是演武面板上的复选框，不是关面板的键。
                # 实测被它连点 5 次：白花 20 秒，还把复选框来回切。
                if "动画" in hit.text:
                    continue
                self.log("    → 关弹窗：点「%s」" % hit.text)
                self.tap(*hit.center)
                break
            else:
                if self.close_info_popup(items):
                    continue
                anchors = self._screen_anchors(items)
                neizheng = self.is_neizheng(items)
                cands = NZ_BACK + SUB_CLOSE if neizheng else SUB_CLOSE
                if anchors:
                    self.log("    · 尝试关闭当前面板 %s" % (anchors[0],))
                    self._try_closes(anchors, cands)
                else:
                    # 认不出的界面：原来只点 cands[0] 就拉倒，于是死等启动超时。
                    # 实测踩到「政务」面板 —— 它的 ✕ 在 (1778,155) 附近，
                    # 正好不是 cands[0]=(1832,53)，300 秒全耗在「等待中…」。
                    # 改成把右上角整串候选都试一遍，每点一次回头确认有没有回主城。
                    self.log("    · 认不出的界面 → 依次试右上角 ✕")
                    for c in cands:
                        self.tap(*c, delay=0.0)
                        time.sleep(1.5)
                        items2, _ = self.ocr("closeunk")
                        if self.is_home(items2):
                            self.log("  已在主城。")
                            return True
                        if self._screen_anchors(items2):
                            break               # 已经切到认得出的面板，交给外层循环
        self.log("  !! 回主城失败（已尝试 %d 次）" % max_rounds)
        return False

    def close_hufu_dialog(self, items: Optional[Sequence[TextItem]] = None) -> None:
        if items is None:
            items, _ = self.ocr("hufu_close")
        hit = self.hit(items, "虎符不足")
        if hit is not None and hit.center[0] < 1200:
            # 标题在中间，关闭键固定右上
            self.tap(*HUFU_DIALOG_CLOSE)
        else:
            self.tap(*HUFU_DIALOG_CLOSE)
        time.sleep(1.5)

    # ------------------------------------------------------------------ 进入各面板

    def open_activity(self) -> bool:
        self.log("  · 打开「活动」面板")
        if not self.click(KW["活动"], xy=HOME_ACTIVITY, tag="act", label="活动",
                          verify=KW["充值好礼"], verify_timeout=10):
            items, _ = self.ocr("act_check")
            if not (self.has(items, "充值好礼") or self.has(items, "精彩活动")):
                return False
        return True

    def open_neizheng(self) -> bool:
        self.log("  · 打开「内政」面板")
        for attempt in range(1, 5):
            # 带放大兜底：内政界面也是深色底 + 美术字，偶发整屏漏读。
            # 漏读会让 is_home/is_neizheng 同时判 False → 白白 to_home() 重来一轮。
            items, _ = self.ocr_multi("nz_pre_%d" % attempt, n=1,
                                      scaled_fallback=True)
            if self.is_neizheng(items):
                return True
            if not self.is_home(items):
                self.to_home()
                continue
            # 底部页签的「内政」是竖排书法体，OCR 基本读不出来（放大 3 倍也读不出）；
            # 而主城别处随时可能出现「内政」二字（资源说明浮层里就有），
            # 所以只在底部页签区域内认它，认不到就用固定坐标。
            hit = None
            for it in items:
                if it.center[1] > 900 and it.center[0] < 800 \
                        and match_score(it.text, "内政") >= 0.62:
                    hit = it
                    break
            if attempt == 2:
                # 有可能是资源说明浮层盖住了页签，先点一下空地把浮层收掉
                self.log("    · 点一下空地收掉可能存在的浮层")
                self.tap(960, 260, delay=0.0)
                time.sleep(1.0)
            if hit is not None:
                self.tap(*hit.center)
            else:
                self.tap(*HOME_TAB_NEIZHENG)
            # 内政面板入场有动画，等它稳一下再确认
            time.sleep(3.5)
            items, _ = self.ocr_multi("nz_post_%d" % attempt, n=1,
                                      scaled_fallback=True)
            if self.is_neizheng(items):
                return True
        self.log("    × 打不开内政面板")
        return False

    SUB_ANCHORS = {
        # 注意：「荣誉」「名师」「贡献」在内政主界面上也可能出现，不能当锚点
        #（实测：内政界面左下「荣誉 786」曾把市井误判成「已经打开」）
        "市井": ("宝物商队", "剩余", "刷新"),
        "税收": ("已征收", "征收中", "加速获取", "征收", "氵正丬攵", "征丬攵"),
        # 特性面板的文字盖在会缓慢移动的插画上，OCR 会整行糊掉，所以别名给得多
        # 注意：不要放「特鞋」——core.fix_ocr 会把插字「鞋」从待识别文本里删掉，
        # 而关键词不删，这样一个 2 字锚点匹配不上任何东西，只会变成死配置。
        "特性": ("剩余特性", "获取1张", "立即获取", "立鼠获取",
                 "剩余特", "半价次数", "半价种", "自选次数"),
        # 演武：实测两个稳定锚点是「同步主城队伍」「挑战」，右下角那块
        # 「扫荡奖励」OCR 会把「扫」读成「归」（两帧都是「归荡奖励」），
        # 但 match_score('归荡奖励','扫荡奖励')=0.67 ≥ 0.62，照样命中。
        "演武": ("同步主城队伍", "守军难度", "跳过战斗动画", "剩余失败次数",
                 "扫荡奖励", "挑战"),
    }

    def _is_neizheng_screen(self, items: Sequence[TextItem]) -> bool:
        """是不是「内政主界面」（一整个房间插画 + 9 个入口）。

        这个界面特别坑：主界面右下角的税收入口下面挂着一个状态标签
        「立即征收N/3」，它含「征收」二字。而主界面本身 OCR 只读得到 8~12 行，
        is_neizheng() 经常凑不够 2 个锚点而返回 False，于是那个标签
        被当成「税收面板内部的征收按钮」——整个税收任务判定成
        「已经在税收面板里了」，静默跳过，一次都不征收，还报成功。

        判据用「立即征收」这个只在主界面出现的标签，双保险再加锚点计数。
        """
        if self.has(items, "立即征收"):
            return True
        return self.is_neizheng(items)

    def _count(self, items: Sequence[TextItem], kw: str) -> int:
        """数一数屏幕上有多少条文字匹配这个关键词。"""
        return sum(1 for it in items if match_score(it.text, kw) >= 0.62)

    def _in_panel(self, name: str, items: Sequence[TextItem]) -> bool:
        """判断是否已经在某个子面板里（避免「内政里的状态字」造成误判）。"""
        if self.is_home(items):
            # 主城永远不是任何子面板。主城上随手点一下资源数字就会弹出说明浮层，
            # 里面写着「内政税收 · 免费征收」「前往市井」——能凑出好几个锚点，
            # 必须先在主城这里一刀切掉。
            return False
        if self._is_neizheng_screen(items):
            # 内政主界面上的「市井 / 税收 / 特性」只是入口名字，不代表面板已打开
            return False
        if name == "税收":
            # ⚠️ 这里**不能**把「强征」当锚点。内政主界面上「税收」入口底下挂着一个
            # 状态标签，文案会变（「立即征收N/3」「可强征N/3」「可领取N/3」），
            # 其中「可强征N/3」含「强征」二字 —— 一旦内政界面 OCR 只读到几行、
            # _is_neizheng_screen 又认不出，就会被判成「已经在税收面板里了」，
            # 于是任务在**内政界面上**开始找征收按钮，静默错位。
            # 实测 2026-09-20：164 帧历史截图里有 1 帧
            # （sub_pre_税收_1_2_027.png）就是这样被误判的；去掉「强征」后归零。
            #
            # 代价是放弃了税收面板里那个付费「20/强征」按钮这个信号 —— 可以接受：
            # 三个格子**永远各有一个「征收」标题**（已征收/征收中/等待中那几格也在），
            # 所以下面的 _count("征收") >= 2 在任何状态下都成立，比「强征」稳得多。
            if self.has(items, "已征收", "征收中", "加速获取", "氵正丬攵", "征丬攵"):
                return True
            # 税收面板有 3 个格子的标题（都含「征收」），主界面最多 1 处
            return self._count(items, "征收") >= 2
        if name == "市井":
            if self.has(items, "宝物商队"):
                return True
            # 「剩余」不能单独当锚点：演武面板上赫然写着「剩余失败次数27」，
            # 会让 open_sub("市井") 以为已经站在市井里（selftest 第 19 组抓到）。
            # 市井面板的剩余次数旁边一定有「刷新」倒计时，两个一起出现才算。
            if self.has(items, "剩余") and self.has(items, "刷新"):
                return True
            return (self.has(items, "市井") and self.has(items, "购买")
                    and self.has(items, "剩余"))
        if name == "特性":
            return self.has(items, *self.SUB_ANCHORS["特性"])
        anchors = self.SUB_ANCHORS.get(name, ())
        return bool(anchors) and self.has(items, *anchors)

    def open_sub(self, name: str) -> bool:
        """在内政里打开某个子面板（市井/税收/特性）。

        每个入口都给「OCR 坐标 + 上下 40px + 固定兜底坐标」多档候选，
        并且用「是否真的进了面板」当校验（而不是命中某个词），
        这样第一次点击没打准时会自动换下一个候选点，而不是误判成功直接收工。
        中途要是把内政关掉了，会自己退主城重新进一次。
        """
        kws = KW[name]
        # 演武入口在单帧里时有时无（实测同一界面一帧读到 (1509,719)、另一帧是 (1527,650)，
        # 也有一帧完全读不到），所以和特性/税收一样走多帧取并集。
        use_multi = name in ("特性", "税收", "演武")
        self.log("  · 进入「%s」" % name)

        def snap(tag):
            # ★ 必须带 scaled_fallback：税收/特性/演武 这三个面板是深色插画底，
            #   原图 OCR 会整屏漏读（实测税收帧只读到 1 行），一旦漏读就会把
            #   「已经进了面板」判成「不在这个面板里」，然后退回主城重进、空转到
            #   最后报「进不去」。放大 2 倍能把同一帧读到 12 行。
            return self.ocr_multi(tag, n=3 if use_multi else 1,
                                  scaled_fallback=True)

        for rnd in range(1, 4):
            items, _ = snap("sub_pre_%s_%d" % (name, rnd))
            if self._in_panel(name, items):
                self.log("    已经在「%s」里了" % name)
                return True
            if not self._is_neizheng_screen(items):
                # 上一次尝试可能把内政关掉了（比如误点了返回键），重新进一次
                self.log("    · 当前不在内政界面，退回主城重新进入")
                if not self.open_neizheng():
                    break
                continue

            self.click(kws, xy=NZ_ENTRY_FALLBACK.get(name), tag="sub" + name,
                       label=name, tries=5, verify_fn=lambda its: self._in_panel(name, its),
                       verify_timeout=5)

            items, _ = snap("sub_re_%s_%d" % (name, rnd))
            if self._in_panel(name, items):
                self.log("    ✓ 已进入「%s」" % name)
                return True

        self.log("    × 进不去「%s」" % name)
        return False

    def open_recruit(self) -> bool:
        self.log("  · 打开「招募」面板")
        for _ in range(3):
            items, _ = self.ocr("rc_pre")
            if self.has(items, *KW["招募"]) and self.has(items, "赛季卡包", "典藏", "典籍", "新势初起"):
                return True
            if self.is_home(items):
                self.tap(*HOME_RECRUIT)
            else:
                self.to_home()
                continue
            time.sleep(3.5)
            items, _ = self.ocr("rc_post")
            if self.has(items, "赛季卡包", "典藏", "典籍", "新势初起", "免费次数"):
                return True
        self.log("    × 打不开招募面板")
        return False

    # ------------------------------------------------------------------ 翻牌动画

    def finish_reveal(self, back_kws: Sequence[str], max_taps: int = 6,
                      tag: str = "reveal") -> bool:
        """抽卡后的翻牌动画：反复点屏幕中央，直到回到指定界面。"""
        for i in range(max_taps):
            items, _ = self.ocr("%s_%d" % (tag, i))
            if self.has(items, *back_kws):
                return True
            sig = self.guard(items)
            if sig == "hufu_lack":
                return False
            # 详情页右上角有小 ✕，翻牌页点中间即可
            if self.has(items, "特性效果", "星级要求", "阵营要求", "兵种", "配点"):
                self.tap(1831, 59)
            else:
                self.tap(*REVEAL_TAP)
            time.sleep(2.0)
        return self.has(self.ocr(tag + "_end")[0], *back_kws)
