(function () {
  // Tabs (WAI-ARIA tabs pattern with arrow-key support)
  document.querySelectorAll('.tabs').forEach(function (set) {
    var tabs = Array.prototype.slice.call(set.querySelectorAll('[role=tab]'));
    function select(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
      });
      if (focus) tab.focus();
    }
    tabs.forEach(function (tab, i) {
      tab.addEventListener('click', function () { select(tab); });
      tab.addEventListener('keydown', function (e) {
        var n = null;
        if (e.key === 'ArrowRight') n = tabs[(i + 1) % tabs.length];
        else if (e.key === 'ArrowLeft') n = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (e.key === 'Home') n = tabs[0];
        else if (e.key === 'End') n = tabs[tabs.length - 1];
        if (n) { e.preventDefault(); select(n, true); }
      });
    });
  });

  // Copy buttons on every code block
  document.querySelectorAll('.code').forEach(function (box) {
    var pre = box.querySelector('pre');
    if (!pre) return;
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'copy';
    b.textContent = 'Copy';
    b.setAttribute('aria-label', 'Copy code to clipboard');
    b.addEventListener('click', function () {
      var text = pre.innerText.replace(/\n$/, '');
      var done = function () {
        b.textContent = 'Copied';
        b.classList.add('ok');
        setTimeout(function () { b.textContent = 'Copy'; b.classList.remove('ok'); }, 1600);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, fallback);
      } else fallback();
      function fallback() {
        var ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); done(); } catch (e) {}
        document.body.removeChild(ta);
      }
    });
    box.appendChild(b);
  });

  // Open a FAQ entry when linked directly
  if (location.hash) {
    var el = document.querySelector(location.hash);
    if (el && el.tagName === 'DETAILS') el.open = true;
  }
})();
