/* Runs only on the isolated preview origin. Everything it reports is untrusted. */
(() => {
  'use strict';
  const origins = JSON.parse(document.getElementById('acc-origins').textContent).map(value => new URL(value).origin);
  const app = document.getElementById('app');
  const outline = document.getElementById('outline');
  let pick = null, bound = null, timer = null;
  const blocked = 'script,style,noscript,input,textarea,select,option,[contenteditable],[hidden],[aria-hidden="true"]';
  const plain = value => value.replace(/[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, ' ').replace(/\s+/g, ' ').trim();
  function send(origin, value) {
    const message = JSON.stringify({v: 1, ...value});
    if (message.length <= 4096) parent.postMessage(message, origin);
  }
  function clear() {
    if (bound) {
      bound.removeEventListener('pointermove', hover, true);
      bound.removeEventListener('click', choose, true);
      bound.removeEventListener('keydown', key, true);
    }
    bound = null;
    pick = null;
    clearTimeout(timer);
    outline.hidden = true;
  }
  function stop(type) {
    if (pick) send(pick.origin, {type, id: pick.id});
    clear();
  }
  function selector(element) {
    const parts = [];
    for (let node = element; node && parts.length < 8; node = node.parentElement) {
      const tag = node.localName;
      if (!/^[a-z][a-z0-9-]{0,63}$/.test(tag)) return null;
      let index = 1, visited = 0;
      for (let sibling = node.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
        if (++visited > 10000) return null;
        if (sibling.localName === tag) index++;
      }
      parts.unshift(`${tag}:nth-of-type(${index})`);
    }
    const path = parts.join(' > ');
    return path.length <= 512 ? path : null;
  }
  function excerpt(element) {
    if (element.closest(blocked)) return '';
    // Walk one node at a time: TreeWalker filters can scan arbitrarily many
    // rejected siblings inside a single nextNode(), bypassing a visit budget.
    let text = '', node = element;
    for (let visited = 0; node && visited < 200 && text.length < 1000; visited++) {
      let descend = true;
      if (node.nodeType === Node.ELEMENT_NODE) {
        const style = node.ownerDocument.defaultView.getComputedStyle(node);
        descend = !node.matches(blocked) && style.display !== 'none' && style.visibility === 'visible' && style.opacity !== '0';
      } else if (node.nodeType === Node.TEXT_NODE) {
        text += node.textContent.slice(0, 1000 - text.length) + ' ';
      }
      let next = descend ? node.firstChild : null;
      if (!next) {
        while (node !== element) {
          if (node.nextSibling) { next = node.nextSibling; break; }
          node = node.parentNode;
        }
      }
      node = next;
    }
    return plain(text).slice(0, 1000);
  }
  function box(element) {
    const rect = element.getBoundingClientRect(), win = element.ownerDocument.defaultView;
    const x = Math.max(0, rect.left), y = Math.max(0, rect.top);
    const width = Math.min(win.innerWidth, rect.right) - x, height = Math.min(win.innerHeight, rect.bottom) - y;
    if (width <= 0 || height <= 0) return null;
    return {x, y, width, height};
  }
  function hover(event) {
    if (!pick || !(event.target instanceof app.contentWindow.Element)) return;
    const rect = box(event.target);
    outline.hidden = !rect;
    if (rect) Object.assign(outline.style, {left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px`});
  }
  function select(element) {
    const path = selector(element), rect = box(element);
    if (!path || !rect) return stop('unavailable');
    send(pick.origin, {type: 'selected', id: pick.id, element: {selector: path, text: excerpt(element), box: rect}});
    clear();
  }
  function choose(event) {
    if (!pick) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.target instanceof app.contentWindow.Element) select(event.target);
  }
  function key(event) {
    if (!pick) return;
    if (event.key === 'Escape' || event.key === 'Enter') {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.key === 'Escape') stop('cancelled');
      else select(bound.activeElement);
    }
  }
  // A document reload/navigation invalidates the armed selection, including same-origin navigation.
  app.addEventListener('load', () => { if (pick) stop('cancelled'); });
  window.addEventListener('message', event => {
    if (event.source !== parent || !origins.includes(event.origin) || typeof event.data !== 'string' || event.data.length > 256) return;
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (!data || data.v !== 1) return;
    if (data.type === 'hello') return send(event.origin, {type: 'ready'});
    if (data.type === 'cancel' && pick && data.id === pick.id) return clear();
    if (data.type !== 'pick' || typeof data.id !== 'string' || !/^[A-Za-z0-9_-]{16,80}$/.test(data.id)) return;
    clear();
    pick = {id: data.id, origin: event.origin};
    try {
      bound = app.contentDocument;
      if (!bound || !bound.body) return stop('unavailable');
      bound.addEventListener('pointermove', hover, true);
      bound.addEventListener('click', choose, true);
      bound.addEventListener('keydown', key, true);
      app.contentWindow.focus();
      timer = setTimeout(() => stop('cancelled'), 30000);
    } catch { stop('unavailable'); }
  });
})();
