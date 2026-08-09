import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { changePassword, logout } from "../../lib/api/auth";
import { ApiError } from "../../lib/api/client";
import { ME_QUERY_KEY, useAuth } from "../auth/useAuth";
import { XIcon } from "../../components/ui/icons";

const inputClass =
  "h-10 w-full rounded-xl border border-line bg-white px-3.5 text-sm text-ink outline-none transition-colors placeholder:text-ink-secondary focus:border-ink-secondary";
const primaryButtonClass =
  "h-10 rounded-xl bg-ink px-5 text-sm text-white transition-opacity hover:opacity-90 disabled:opacity-40";
const secondaryButtonClass =
  "h-10 rounded-xl border border-line bg-white px-5 text-sm text-ink transition-colors hover:border-ink-secondary/50 disabled:opacity-40";
const dangerButtonClass =
  "h-10 rounded-xl border border-red-200 bg-white px-5 text-sm text-red-600 transition-colors hover:bg-red-50 disabled:opacity-40";

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "操作失败，请稍后重试";
}

/**
 * Account management: profile info, password change, account switch and
 * sign-out. Reached from the sidebar user menu ("账号管理").
 *
 * Note: a successful password change revokes every session server-side
 * (session_version bump), so the user is sent back to the login page.
 */
export function AccountPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useAuth();

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [changing, setChanging] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);

  async function backToLogin() {
    await queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
    navigate("/login", { replace: true });
  }

  async function handleChangePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice(null);
    if (newPassword.length < 12) {
      setNotice({ ok: false, text: "新密码至少需要 12 位" });
      return;
    }
    if (newPassword !== confirmPassword) {
      setNotice({ ok: false, text: "两次输入的新密码不一致" });
      return;
    }
    setChanging(true);
    try {
      await changePassword(currentPassword, newPassword);
      // The backend revoked every session including this one; re-login is required.
      window.alert("密码已修改，请使用新密码重新登录");
      await backToLogin();
    } catch (err) {
      setNotice({
        ok: false,
        text: err instanceof ApiError && err.status === 401 ? "当前密码不正确" : `修改失败：${errorText(err)}`,
      });
      setChanging(false);
    }
  }

  async function handleSignOut(confirmText: string) {
    if (loggingOut || !window.confirm(confirmText)) return;
    setLoggingOut(true);
    try {
      await logout();
    } catch {
      // The session may already be invalid; proceed to the login page anyway.
    }
    await backToLogin();
  }

  const displayName = user?.email.split("@")[0] ?? "";

  return (
    <div className="w-full max-w-[720px] px-8 py-10">
      {/* Close: back to the chat home */}
      <button
        type="button"
        title="关闭账号管理"
        aria-label="关闭账号管理"
        onClick={() => navigate("/")}
        className="fixed right-6 top-6 flex h-9 w-9 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
      >
        <XIcon size={18} />
      </button>

      <h1 className="text-[22px] font-semibold text-ink">账号管理</h1>

      {/* Account info */}
      <section className="mt-8">
        <h2 className="mb-1 text-base font-semibold text-ink">账号信息</h2>
        <p className="mb-4 text-sm text-ink-secondary">当前登录的账号。</p>
        <div className="flex items-center gap-3 rounded-xl border border-line bg-white px-4 py-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-amber-200 to-orange-300 text-sm font-semibold text-ink">
            {displayName.charAt(0).toUpperCase()}
          </div>
          <span className="min-w-0 flex-1 truncate text-sm text-ink">{user?.email ?? "加载中…"}</span>
          {user?.role && (
            <span className="rounded-md bg-ink px-1.5 py-0.5 text-[10px] font-medium text-white">{user.role}</span>
          )}
        </div>
      </section>

      {/* Change password */}
      <section className="mt-10">
        <h2 className="mb-1 text-base font-semibold text-ink">修改密码</h2>
        <p className="mb-4 text-sm text-ink-secondary">修改成功后所有已登录会话都会失效，需要使用新密码重新登录。</p>
        <form onSubmit={(event) => void handleChangePassword(event)} className="flex max-w-[400px] flex-col gap-3">
          <input
            type="password"
            value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)}
            placeholder="当前密码"
            autoComplete="current-password"
            className={inputClass}
          />
          <input
            type="password"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
            placeholder="新密码（至少 12 位）"
            autoComplete="new-password"
            className={inputClass}
          />
          <input
            type="password"
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
            placeholder="确认新密码"
            autoComplete="new-password"
            className={inputClass}
          />
          <div>
            <button
              type="submit"
              disabled={changing || !currentPassword || !newPassword || !confirmPassword}
              className={primaryButtonClass}
            >
              {changing ? "提交中…" : "确认修改"}
            </button>
          </div>
        </form>
        {notice && <p className={`mt-3 text-sm ${notice.ok ? "text-emerald-600" : "text-red-600"}`}>{notice.text}</p>}
      </section>

      {/* Switch account */}
      <section className="mt-10">
        <h2 className="mb-1 text-base font-semibold text-ink">切换账号</h2>
        <p className="mb-4 text-sm text-ink-secondary">退出当前账号，并使用其他账号重新登录。</p>
        <button
          type="button"
          disabled={loggingOut}
          onClick={() => void handleSignOut("将退出当前账号并返回登录页，继续？")}
          className={secondaryButtonClass}
        >
          切换账号
        </button>
      </section>

      {/* Sign out */}
      <section className="mt-10">
        <h2 className="mb-1 text-base font-semibold text-ink">退出账号</h2>
        <p className="mb-4 text-sm text-ink-secondary">退出当前账号的登录状态。</p>
        <button
          type="button"
          disabled={loggingOut}
          onClick={() => void handleSignOut("确定退出当前账号？")}
          className={dangerButtonClass}
        >
          退出账号
        </button>
      </section>
    </div>
  );
}
