// 统一请求层：同源地址补全 + 鉴权头注入 + 访问令牌过期自动续期 + 401 强制定位登录。
// 被 chat / review 两个页面共享。令牌来源为 shared/state.js 的 auth 对象（单一事实来源）。

import { auth } from "./state.js";

export const API_BASE = window.location.origin;

export function absUrl(u) {
  if (!u) return u;
  if (u.startsWith("http://") || u.startsWith("https://")) return u;
  return API_BASE + u;
}

const REFRESH_KEY = "med_refresh";
const ACCOUNTS_KEY = "med_accounts";

// 多账号本地记忆：记住已登录的 token / refresh，便于静默重登与账号切换。
export function rememberAccount(u, token, refresh) {
  let acc = {};
  try {
    acc = JSON.parse(localStorage.getItem(ACCOUNTS_KEY) || "{}");
  } catch (e) {
    acc = {};
  }
  acc[u] = { token: token, refresh: refresh || "" };
  localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(acc));
}

export function getAccounts() {
  try {
    return JSON.parse(localStorage.getItem(ACCOUNTS_KEY) || "{}");
  } catch (e) {
    return {};
  }
}

// 统一带鉴权的请求封装：访问令牌过期(401)时自动用刷新令牌续期一次，失败则强制重新登录。
export async function apiFetch(url, opts = {}) {
  opts.headers = Object.assign(
    { "Content-Type": "application/json" },
    opts.headers || {}
  );
  if (auth.token) opts.headers["Authorization"] = auth.token;

  let r = await fetch(absUrl(url), opts);

  if (r.status === 401 && auth.refresh) {
    try {
      const rf = await fetch(absUrl("/auth/refresh"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: auth.refresh }),
      });
      if (rf.ok) {
        const d = await rf.json();
        auth.token = "Bearer " + d.access_token;
        auth.refresh = d.refresh_token || "";
        if (auth.refresh) localStorage.setItem(REFRESH_KEY, auth.refresh);
        rememberAccount(auth.user, auth.token, auth.refresh);
        opts.headers["Authorization"] = auth.token;
        r = await fetch(absUrl(url), opts);
      } else {
        auth.refresh = "";
        localStorage.removeItem(REFRESH_KEY);
      }
    } catch (e) {
      auth.refresh = "";
      localStorage.removeItem(REFRESH_KEY);
    }
  }

  if (r.status === 401) {
    auth.token = "";
    auth.refresh = "";
    localStorage.removeItem(REFRESH_KEY);
    localStorage.removeItem("med_token");
    const mask = document.getElementById("loginMask");
    if (mask) mask.style.display = "flex";
    throw new Error("unauthorized");
  }
  return r;
}
