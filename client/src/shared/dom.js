// 轻量 DOM 工具：ID 选择、HTML 转义、时间格式化。
// 被 chat / review 两个页面共享，避免重复实现（此前两份内联脚本各自复制了一份 esc/$）。

export function $(id) {
  return document.getElementById(id);
}

// 所有用户可控文本进入 innerHTML 前必须经过 esc()，防止存储型 XSS。
export function esc(x) {
  return String(x == null ? "" : x).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

export function fmtTime(s) {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d)) return s;
  const p = (n) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}`
  );
}
