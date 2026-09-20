/* 控制台的少量交互 —— 刻意**全部放在这个外部文件里**。
 *
 * ⚠️ 页面里不许再写内联脚本，也不许再用内联事件处理器（onclick= / onsubmit= …）。
 *    原因在 server/app/main.py 的 CSP：script-src 只放 'self'，内联脚本会被
 *    浏览器**直接拒绝执行**。
 *
 * ★ 这不是洁癖，是一个真实踩过的坑：
 *   之前这里的逻辑是内联写的，而 CSP 是 `script-src 'none'` —— 于是
 *   「角色下拉联动」「在线状态自动刷新」「**删除/轮换令牌前的二次确认**」
 *   全部静默失效，页面上不报任何错（只有浏览器控制台里才有）。
 *   二次确认失效尤其危险：删客户端、删运行记录、轮换采集端令牌
 *   都是不可逆操作，却没有一个确认框。
 *
 *    修法就是现在这样：外部 JS 文件（同源 → 'self' 放行）+ data-* 属性传参，
 *    CSP 收紧到 `script-src 'self'`，既不放松安全性，功能也真的能跑。
 */
(function () {
  'use strict';

  /* ---------------------------------------------------------- 破坏性操作确认
   * 用法：<form method="post" action="…" data-confirm="真的要删吗？">
   * 比内联 onsubmit 好在：① CSP 放行；② 文案写在 HTML 上，一眼能看到哪些
   * 操作是危险的（搜 `data-confirm` 就能把全部危险操作列出来）。
   */
  function wireConfirm() {
    var forms = document.querySelectorAll('form[data-confirm]');
    for (var i = 0; i < forms.length; i++) {
      forms[i].addEventListener('submit', function (e) {
        var msg = this.getAttribute('data-confirm');
        if (msg && !window.confirm(msg)) {
          e.preventDefault();
          return false;
        }
        return true;
      });
    }
  }

  /* -------------------------------------------------- 客户端：账号/角色下拉联动
   * 用法：<select data-cli-role-filter="{{ c.id }}">，
   *       角色下拉的 id 为 roles-{{ c.id }}，其中每个 option 带 data-acc="账号id"。
   * 全量角色已经写在 DOM 的 data-* 里了，所以这里不需要再发一次请求。
   */
  function filterRoles(sel) {
    var box = document.getElementById('roles-' + sel.getAttribute('data-cli-role-filter'));
    if (!box) return;
    var acc = sel.value;
    var opts = box.querySelectorAll('option[data-acc]');
    for (var i = 0; i < opts.length; i++) {
      var o = opts[i];
      var keep = (acc === '' || o.getAttribute('data-acc') === acc);
      o.hidden = !keep;
      if (!keep && o.selected) { box.value = ''; }
    }
    // 没选账号 → 角色下拉整体不可用（避免选出一个不属于任何账号的角色）
    box.disabled = (acc === '');
  }

  function wireRoleFilter() {
    var sels = document.querySelectorAll('select[data-cli-role-filter]');
    for (var i = 0; i < sels.length; i++) {
      (function (sel) {
        sel.addEventListener('change', function () { filterRoles(sel); });
        // 进页面先按当前值收敛一次，避免刚打开就看到别的账号的角色
        filterRoles(sel);
      })(sels[i]);
    }
  }

  /* ------------------------------------------------------------ 下拉即提交
   * 用法：<select data-auto-submit>（角色执行页的「最近 N 天」）
   */
  function wireAutoSubmit() {
    var sels = document.querySelectorAll('select[data-auto-submit]');
    for (var i = 0; i < sels.length; i++) {
      sels[i].addEventListener('change', function () { this.form.submit(); });
    }
  }

  /* ---------------------------------------------------- 客户端在线状态自动刷新
   * 心跳 30 秒一拍，15 秒刷一次足够跟得上；只在有在线统计的页面（客户端页）跑。
   */
  function text(id, v) {
    var e = document.getElementById(id);
    if (e) { e.textContent = v; }
  }

  function refreshOnline() {
    fetch('/api/clients', {headers: {'Accept': 'application/json'}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) { return; }
        if (d.online) {
          text('n-online', d.online.online);
          text('n-offline', d.online.offline);
          text('n-paused', d.online.paused);
          text('n-total', d.online.total);
        }
        var list = d.clients || [];
        for (var i = 0; i < list.length; i++) {
          var c = list[i];
          var dot = document.querySelector('#cli-' + c.id + ' .dot');
          if (dot) {
            dot.className = 'dot ' + (c.online ? 'on' : 'off');
          }
          text('age-' + c.id, c.online ? '在线' : '离线');
          // 实时通道指示灯：连上=后台的指令/任务秒到；没连=按心跳节奏（最多等一拍）
          text('rt-' + c.id, c.realtime ? '实时' : '心跳');
          var rt = document.getElementById('rt-' + c.id);
          if (rt) { rt.className = 'chip rt ' + (c.realtime ? 'on' : 'off'); }
          text('note-' + c.id, c.note || '');
          // 有结果了就把「等待中」的探测提示收掉
          var pb = document.getElementById('probe-' + c.id);
          if (pb && c.probe_status && c.probe_status !== 'pending'
              && c.probe_status !== 'running') {
            pb.style.display = 'none';
          }
          // 探测状态标签：实时通道接通/断开会让「已推送 / 等待心跳」这两种说法变来变去，
          // 所以这里跟着刷一下，免得页面上写着「等待心跳」其实早推过去了。
          var pp = document.getElementById('probe-pill-' + c.id);
          if (pp && c.probe_status === 'pending') {
            pp.textContent = c.realtime ? '已推送，等待执行' : '等待客户端心跳';
          }
        }
      })
      .catch(function () { /* 网络抖一下就跳过这一拍，不打扰用户 */ });
  }

  function wireAutoRefresh() {
    if (!document.getElementById('n-online')) { return; }   // 这个页面没有在线统计
    refreshOnline();
    setInterval(refreshOnline, 15000);
  }

  function boot() {
    wireConfirm();
    wireRoleFilter();
    wireAutoSubmit();
    wireAutoRefresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
