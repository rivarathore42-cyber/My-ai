/*
 * Galaxy AI chat page. One script, no framework.
 * The session is an HttpOnly cookie set by pairing, so this script never sees a token and keeps nothing in browser storage.
 * Every reply is rendered with textContent, never as HTML.
 * It defines the hooks the voice and permission scripts call: galaxyVoiceSend, galaxyHeaders and galaxyUser.
 * voice_ui.js defines galaxyOnAction and galaxyOnReply; this file calls them.
 */
(function () {
  'use strict';

  var NO_NETWORK = 'Server reachable nahi hai. Free hosting par server so raha ho sakta hai: 1 minute baad page dobara load karein. Link bhi check karein.';
  var NOT_JSON = 'Server ne page ki bajaye kuch aur bheja (tunnel ka warning page ho sakta hai). Link khol kar dobara try karein.';
  var busy = false;

  function $(id) { return document.getElementById(id); }

  function setStatus(kind, text) {
    var pill = $('status');
    pill.setAttribute('data-kind', kind);
    pill.textContent = text;
  }

  function showPairing(show) {
    $('pairCard').hidden = !show;
    $('composer').hidden = show;
    $('unpair').hidden = show;
    if (show) { $('pairCode').focus(); }
  }

  // One request helper. Resolves to { ok, status, body }, { networkError: true } or { parseError: true, status }.
  function call(path, options) {
    var opts = options || {};
    var init = { method: opts.method || 'GET', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } };
    if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); }
    return fetch(path, init).then(function (res) {
      return res.text().then(function (raw) {
        var body;
        try { body = JSON.parse(raw); } catch (e) { return { parseError: true, status: res.status }; }
        return { ok: res.ok, status: res.status, body: body };
      }).catch(function () { return { parseError: true, status: res.status }; });
    }, function () { return { networkError: true }; });
  }

  function bubble(role, text) {
    var list = $('messages');
    var wrap = document.createElement('div');
    wrap.className = 'msg ' + role;
    var body = document.createElement('div');
    body.className = 'bubble';
    body.textContent = text;
    wrap.appendChild(body);
    list.appendChild(wrap);
    list.scrollTop = list.scrollHeight;
    return wrap;
  }

  function refreshMe() {
    return call('/api/me').then(function (r) {
      if (r.networkError) { setStatus('offline', 'Server offline'); showPairing(false); return false; }
      if (r.parseError) { setStatus('offline', 'Server ne valid jawab nahi diya'); return false; }
      if (r.ok && r.body && r.body.kind) {
        setStatus('paired', r.body.mode === 'local' ? 'Local mode' : 'Paired');
        showPairing(false);
        return true;
      }
      setStatus('locked', 'Pairing needed');
      showPairing(true);
      return false;
    });
  }

  function pair() {
    var code = $('pairCode').value.replace(/\D/g, '');
    if (code.length !== 8) { $('pairMsg').textContent = 'Code 8 digits ka hona chahiye.'; return; }
    $('pairBtn').disabled = true;
    call('/api/pair', { method: 'POST', body: { code: code } }).then(function (r) {
      $('pairBtn').disabled = false;
      if (r.networkError) { $('pairMsg').textContent = NO_NETWORK; return; }
      if (r.parseError) { $('pairMsg').textContent = NOT_JSON; return; }
      if (!r.ok) { $('pairMsg').textContent = (r.body && r.body.text) || 'Pairing nahi ho paya.'; return; }
      $('pairCode').value = '';
      $('pairMsg').textContent = '';
      refreshMe().then(function (ok) {
        if (ok) { bubble('bot', 'Namaste! Device pair ho gaya. Kuch bhi poochhiye.'); }
      });
    });
  }

  // Sends one message. Returns false when a send is already in progress.
  function send(text, source) {
    var clean = String(text || '').trim();
    if (busy || !clean) { return false; }
    busy = true;
    $('send').disabled = true;
    bubble('user', clean);
    var waiting = bubble('bot pending', 'Soch raha hoon…');
    var payload = { message: clean, conversation_id: 'default', source: source || 'text' };
    call('/api/chat', { method: 'POST', body: payload }).then(function (r) {
      waiting.remove();
      var reply;
      if (r.networkError) {
        reply = { text: NO_NETWORK, ok: false };
      } else if (r.parseError) {
        reply = { text: NOT_JSON, ok: false };
      } else if (r.body && typeof r.body.text === 'string' && r.body.text.trim()) {
        reply = r.body;
      } else {
        reply = { text: 'Server se valid jawab nahi mila (status ' + r.status + ').', ok: false };
      }
      bubble(reply.ok === false ? 'bot error' : 'bot', reply.text);
      if (r.status === 401) { refreshMe(); }
      if (reply.action && window.galaxyOnAction) { window.galaxyOnAction(reply.action); }
      if (window.galaxyOnReply) { window.galaxyOnReply(reply.text); }
    }).then(function () {
      busy = false;
      $('send').disabled = false;
    });
    return true;
  }

  // Hooks used by voice_ui.js and permission_modal.js.
  window.galaxyVoiceSend = function (text) {
    window.__galaxySrc = 'voice';
    var accepted = send(text, 'voice');
    window.__galaxySrc = 'text';
    return accepted;
  };
  window.galaxyHeaders = function () { return { 'Content-Type': 'application/json' }; };
  window.galaxyUser = function () { return ''; };   // the identity comes from the session, never from the page

  $('send').onclick = function () {
    var box = $('input');
    if (send(box.value, 'text')) { box.value = ''; box.style.height = ''; }
  };
  $('input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('send').click(); }
  });
  $('input').addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 140) + 'px';
  });
  $('pairBtn').onclick = pair;
  $('pairCode').addEventListener('keydown', function (e) { if (e.key === 'Enter') { pair(); } });
  $('unpair').onclick = function () {
    call('/api/unpair', { method: 'POST' }).then(function () {
      bubble('bot', 'Device unpair ho gaya. Dobara chat ke liye pair karein.');
      refreshMe();
    });
  };

  refreshMe();
})();
