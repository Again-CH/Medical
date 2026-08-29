// 全局认证态：单一事实来源。
// 各页面（chat / review）在登录后写入 auth.*，shared/api.js 在发起请求与刷新令牌时读取，
// 避免出现「页面局部变量」与「apiFetch 内部令牌」不同步导致的 401 死循环。
export const auth = {
  token: "",
  user: "",
  refresh: "",
};
