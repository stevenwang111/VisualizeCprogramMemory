/* ============================================================
   oplog.js —— 操作记录器（前端观测层，纯旁路）

   原则：只观测、不干预。
   - 所有逻辑都在 try/catch 里，写日志失败绝不影响调试器；
   - 不修改任何现有按钮的点击处理函数，用捕获阶段监听；
   - 按钮禁用/启用、busy 状态都靠观察 DOM 和网络请求推断，
     不去碰 app.js 的内部 state。
   ============================================================ */
(function () {
  'use strict';

  var ENDPOINT = '/api/oplog';
  var TOOLBAR_IDS = [
    'btn-debug', 'btn-next', 'btn-step', 'btn-finish',
    'btn-goto', 'btn-continue', 'btn-stop', 'btn-restart', 'btn-clear'
  ];
  var btnState = Object.create(null);
  var ready = false;

  /* ---------------- 写日志：发后不理 ---------------- */
  function emit(type, message, extra) {
    try {
      var payload = { type: type, message: message };
      if (extra) {
        for (var k in extra) {
          if (Object.prototype.hasOwnProperty.call(extra, k)) payload[k] = extra[k];
        }
      }
      var body = JSON.stringify(payload);

      // sendBeacon 最适合「发完就走」，关标签页时也不会丢；
      // 老浏览器或队列满时退回 fetch(keepalive)。
      if (navigator.sendBeacon) {
        try {
          var blob = new Blob([body], { type: 'application/json' });
          if (navigator.sendBeacon(ENDPOINT, blob)) return;
        } catch (e) { /* fall through */ }
      }
      fetch(ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body,
        keepalive: true
      })['catch'](function () {});
    } catch (e) { /* 记录器自身故障必须静默 */ }
  }

  function labelOf(btn) {
    var id = btn.id || '';
    var text = (btn.textContent || '').replace(/\s+/g, ' ').trim();
    return (id || 'button') + (text ? '「' + text + '」' : '');
  }

  /* ---------------- 1. 用户点击的按钮 ----------------
     捕获阶段监听，app.js 里的 click 处理完全不用改。 */
  function watchClicks() {
    document.addEventListener('click', function (e) {
      try {
        var t = e.target;
        if (!t || !t.closest) return;
        var btn = t.closest('button');
        if (!btn) return;
        var inToolbar = !!(btn.closest && btn.closest('.controls'));
        emit('CLICK', '点击按钮 ' + labelOf(btn), {
          id: btn.id || '',
          toolbar: inToolbar ? '1' : '0'
        });
      } catch (err) { /* ignore */ }
    }, true);
  }

  /* 快捷键也算用户输入，记下来能和按钮点击区分开。 */
  function watchKeys() {
    var keys = {
      F5: 'debug', F7: 'next', F8: 'step', F4: 'continue',
      F6: 'stop', 'Ctrl+F8': 'finish', 'Ctrl+F5': 'goto'
    };
    document.addEventListener('keydown', function (e) {
      try {
        if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
        var name = e.key;
        if ((e.ctrlKey || e.metaKey) && (name === 'F8' || name === 'F5')) {
          name = 'Ctrl+' + name;
        }
        if (keys[name]) emit('KEY', '快捷键 ' + name + '（' + keys[name] + '）', { key: name });
      } catch (err) { /* ignore */ }
    }, true);
  }

  /* 下拉框 / 复选框的改动也顺手记一下，频率很低。 */
  function watchInputs() {
    document.addEventListener('change', function (e) {
      try {
        var el = e.target;
        if (!el || !el.id) return;
        if (el.tagName === 'SELECT' || el.tagName === 'INPUT') {
          emit('INPUT', '修改 ' + el.id + ' = ' + (el.value !== undefined ? el.value : el.checked),
            { id: el.id, value: String(el.value !== undefined ? el.value : el.checked) });
        }
      } catch (err) { /* ignore */ }
    }, true);
  }

  /* ---------------- 2. 上排按钮禁用/启用变化 ---------------- */
  function watchButtons() {
    TOOLBAR_IDS.forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      btnState[id] = !!el.disabled;
      try {
        var mo = new MutationObserver(function () {
          try {
            var now = !!el.disabled;
            if (now === btnState[id]) return;   // 同一个值重复赋值不算变化
            btnState[id] = now;
            emit('BTN', '按钮 ' + id + (now ? ' 禁用' : ' 启用'), {
              id: id,
              state: now ? 'disabled' : 'enabled'
            });
          } catch (err) { /* ignore */ }
        });
        mo.observe(el, { attributes: true, attributeFilter: ['disabled'] });
      } catch (err) { /* MutationObserver 不支持就算了 */ }
    });
  }

  /* ---------------- 3. busy 状态进退 ----------------
     app.js 里 state.busy 被包在 IIFE 内，外部拿不到。但它和
     /api/command 请求的生命周期严格一致：发请求前进 busy，
     finally 里出 busy。所以包一层 fetch 就能精确观测，且对
     原调用完全透明（原样返回同一个 Promise）。 */
  function watchBusy() {
    try {
      var origFetch = window.fetch;
      if (typeof origFetch !== 'function') return;

      window.fetch = function (input, init) {
        var url = '';
        try {
          url = typeof input === 'string' ? input : (input && input.url) || String(input);
        } catch (e) { /* ignore */ }

        if (url.indexOf('/api/command') === -1) {
          return origFetch.apply(this, arguments);
        }

        var cmd = '';
        try {
          var body = init && init.body;
          if (typeof body === 'string') cmd = JSON.parse(body).cmd || '';
        } catch (e) { /* ignore */ }

        emit('BUSY', 'busy 进入', { cmd: cmd });

        var promise;
        try {
          promise = origFetch.apply(this, arguments);
        } catch (err) {
          emit('BUSY', 'busy 退出（发送失败）', { cmd: cmd });
          throw err;
        }
        try {
          Promise.resolve(promise).then(
            function (resp) {
              emit('BUSY', 'busy 退出', { cmd: cmd });
              // 顺手把后端怎么回的记下来（ok / waiting / killed）。
              // 用 clone() 读副本，原 response 仍然交给 app.js，
              // 所以这段只观测、不改变任何行为。
              try {
                if (resp && typeof resp.clone === 'function') {
                  resp.clone().json().then(function (d) {
                    try {
                      emit('CMDRES', '命令返回 ' + cmd, {
                        cmd: cmd,
                        ok: d && d.ok ? 1 : 0,
                        waiting: d && d.waiting ? 1 : 0,
                        killed: d && d.killed !== undefined ? (d.killed ? 1 : 0) : '',
                        msg: (d && d.message) || ''
                      });
                    } catch (e) { /* ignore */ }
                  })['catch'](function () {});
                }
              } catch (e) { /* ignore */ }
            },
            function () { emit('BUSY', 'busy 退出（请求失败）', { cmd: cmd }); }
          );
        } catch (e) { /* ignore */ }
        return promise;
      };
    } catch (err) { /* ignore */ }
  }

  /* ---------------- 4. 被加载的文件 ----------------
     顶栏 #prog-name 由 fetchState() 填入，观察它的文本变化
     即可知道界面当前加载的是哪个文件。 */
  function watchProgram() {
    var el = document.getElementById('prog-name');
    if (!el) return;
    var last = (el.textContent || '').trim();
    try {
      var mo = new MutationObserver(function () {
        try {
          var cur = (el.textContent || '').trim();
          if (cur && cur !== '—' && cur !== last) {
            emit('FILE', '加载文件 ' + cur, { file: cur });
          }
          last = cur;
        } catch (err) { /* ignore */ }
      });
      mo.observe(el, { childList: true, characterData: true, subtree: true });
    } catch (err) { /* ignore */ }
  }

  /* ---------------- 5. 状态栏：程序停在哪 / 处于什么阶段 ----------------
     app.js 的 state 封在 IIFE 里拿不到，但状态栏文字是它渲染出来的：
     #status-loc 会变成「文件名:行号 函数」，#status-badge 会是
     未执行 / 运行中 / 就绪 / 程序已退出。
     盯这两个元素的文本变化，等于间接拿到了「每条命令之后程序停在哪」——
     复盘「点了停止但界面像没刷新」这类问题时，这行是决定性的证据。 */
  function watchText(id, type, label) {
    var el = document.getElementById(id);
    if (!el) return;
    var last = (el.textContent || '').trim();
    try {
      var mo = new MutationObserver(function () {
        try {
          var cur = (el.textContent || '').trim();
          if (cur === last) return;
          last = cur;
          emit(type, label + ' ' + cur, { text: cur });
        } catch (err) { /* ignore */ }
      });
      mo.observe(el, { childList: true, characterData: true, subtree: true });
    } catch (err) { /* ignore */ }
  }

  function watchStatus() {
    watchText('status-loc', 'LOC', '位置');
    watchText('status-badge', 'PHASE', '阶段');
  }

  /* ---------------- 6. 页面异常 ----------------
     前端真正报错时，日志里应该留一条，而不是让人对着空白界面猜。 */
  function watchErrors() {
    window.addEventListener('error', function (e) {
      try {
        emit('ERROR', '页面异常 ' + (e.message || ''), {
          where: (e.filename || '') + ':' + (e.lineno || 0)
        });
      } catch (err) { /* ignore */ }
    });
    window.addEventListener('unhandledrejection', function (e) {
      try {
        emit('ERROR', '未处理的 Promise 拒绝', { msg: String(e.reason) });
      } catch (err) { /* ignore */ }
    });
  }

  function init() {
    if (ready) return;
    ready = true;
    // 先包 fetch，再装观察器：这样 app.js 的 init() 里发出的
    // /api/state 和后续命令都能被覆盖到。
    watchBusy();
    watchProgram();
    watchButtons();
    watchClicks();
    watchKeys();
    watchInputs();
    watchStatus();
    watchErrors();
    emit('PAGE', '操作记录器就绪');
  }

  // 脚本在 </body> 前加载，元素已经解析完；万一被挪到 head 里，
  // 就等 DOMContentLoaded 再装观察器，避免拿到 null 元素。
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
