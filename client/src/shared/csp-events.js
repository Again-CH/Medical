// 严格 CSP 兼容的事件委托（script-src 'self' 'nonce-' 下，内联事件处理器 onclick="" 会被拦截）。
// 所有交互通过 data-action* 属性声明，由全局委托统一分发，页面只需 registerActions({...}) 注册行为。

window.__ACTIONS = window.__ACTIONS || {};

(function () {
  function target(e, attr) {
    return e.target && e.target.closest ? e.target.closest("[" + attr + "]") : null;
  }
  document.addEventListener("click", function (e) {
    const el = target(e, "data-action");
    if (!el) return;
    const fn = window.__ACTIONS[el.getAttribute("data-action")];
    if (fn) {
      e.preventDefault();
      fn(el, e);
    }
  });
  document.addEventListener("keydown", function (e) {
    const el = target(e, "data-keydown-enter");
    if (!el || e.key !== "Enter" || e.shiftKey) return;
    const fn = window.__ACTIONS[el.getAttribute("data-keydown-enter")];
    if (fn) {
      e.preventDefault();
      fn(el, e);
    }
  });
  document.addEventListener("input", function (e) {
    const el = target(e, "data-action-input");
    if (!el) return;
    const fn = window.__ACTIONS[el.getAttribute("data-action-input")];
    if (fn) fn(el, e);
  });
  document.addEventListener("change", function (e) {
    const el = target(e, "data-action-change");
    if (!el) return;
    const fn = window.__ACTIONS[el.getAttribute("data-action-change")];
    if (fn) fn(el, e);
  });
})();

// 把一组 { actionName: handler } 合并进全局动作表。
export function registerActions(map) {
  Object.assign(window.__ACTIONS, map);
}
