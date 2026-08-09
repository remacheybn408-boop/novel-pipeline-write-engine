import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { clearTabToken, fetchMe, fetchRegistrationStatus, getTabToken, login, register, setup } from "../../lib/api/auth";
import { ApiError } from "../../lib/api/client";
import { ME_QUERY_KEY } from "./useAuth";

type Mode = "login" | "setup" | "register";

export type AccountEntry = "setup" | "register" | "none" | "loading";

/**
 * Which account-creation entry the login page should offer:
 * - fresh instance (no users yet) -> setup creates the first (admin) account
 * - initialized + registration open -> register a regular user
 * - initialized + registration closed -> no creation entry, plain login only
 * Status still loading / failed -> "loading": show no creation entry at all,
 * so a closed instance never flashes a 创建账号 button while the status
 * resolves.
 */
export function accountEntryFor(status?: { enabled: boolean; initialized: boolean } | null): AccountEntry {
  if (!status) {
    return "loading";
  }
  if (!status.initialized) {
    return "setup";
  }
  return status.enabled ? "register" : "none";
}

/**
 * /login signs in; setup mode creates the very first account
 * (POST /api/v1/auth/setup); register mode creates a regular USER account
 * (POST /api/v1/auth/register, only when the instance allows registration).
 * Note: setup/register do NOT create a session — the user signs in afterwards.
 */
export function LoginPage() {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // Public instance flags: pick the right account-creation entry (setup for a
  // fresh instance, register when allowed, none once initialized and closed).
  const registrationStatus = useQuery({
    queryKey: ["auth", "registration-status"],
    queryFn: fetchRegistrationStatus,
    retry: false,
    staleTime: 60_000,
  });
  const accountEntry = accountEntryFor(registrationStatus.data ?? null);
  const registrationEnabled = registrationStatus.data?.enabled === true;

  // A tab already holding a bearer token goes straight into the app; the
  // token is verified first so an expired/revoked one is dropped and the
  // form shows instead of bouncing between /login and the shell guard.
  useEffect(() => {
    if (!getTabToken()) return;
    let cancelled = false;
    fetchMe()
      .then(() => {
        if (!cancelled) navigate("/", { replace: true });
      })
      .catch(() => clearTabToken());
    return () => {
      cancelled = true;
    };
  }, [navigate]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setNotice(null);

    if (password.length < 12) {
      setError("密码长度至少为 12 位");
      return;
    }

    setSubmitting(true);
    try {
      if (mode === "setup" || mode === "register") {
        if (mode === "setup") {
          await setup(email, password);
        } else {
          await register(email, password);
        }
        // Setup/register succeed but issue no cookie — fall back to sign-in mode.
        setMode("login");
        setPassword("");
        setNotice("账号已创建，请登录");
      } else {
        await login(email, password);
        await queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
        navigate("/", { replace: true });
      }
    } catch (err) {
      if (err instanceof ApiError) {
        if (mode === "setup" && err.status === 409) {
          setError(
            registrationEnabled
              ? "此实例已创建过管理员账号，请直接登录或注册新用户"
              : "此实例已初始化过账号，请直接登录；忘记密码需在服务器上重置账号",
          );
          setMode("login");
        } else if (mode === "register" && err.status === 409) {
          setError("该邮箱已注册，请直接登录");
          setMode("login");
        } else if (mode === "register" && err.status === 403) {
          setError("此实例未开放注册");
        } else if (err.status === 401) {
          setError("邮箱或密码不正确");
        } else {
          setError(err.message);
        }
      } else {
        setError("发生未知错误，请稍后重试");
      }
    } finally {
      setSubmitting(false);
    }
  }

  const inputClass =
    "h-11 w-full rounded-xl border border-line bg-white px-3.5 text-sm text-ink outline-none transition-colors placeholder:text-ink-secondary focus:border-ink-secondary";

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-[#f7f7f7] bg-[url(/bg-ink.jpg)] bg-cover bg-[position:center_bottom] bg-no-repeat px-4">
      {/* Translucent white veil over the ink background to keep the tone neutral */}
      <div aria-hidden className="pointer-events-none absolute inset-0 bg-white/25" />
      <div className="relative w-full max-w-[360px]">
        <div className="mb-10 text-center">
          <h1 className="text-[40px] font-extrabold leading-none tracking-tight text-ink">
            ProseForge
          </h1>
          <p className="mt-3 text-sm text-ink-secondary">
            {mode === "login" ? "登录到你的写作工作台" : mode === "setup" ? "创建第一个账号" : "注册新账号"}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <input
            type="email"
            required
            autoComplete="email"
            placeholder="邮箱"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className={inputClass}
          />
          <input
            type="password"
            required
            minLength={12}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            placeholder="密码（至少 12 位）"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={inputClass}
          />

          {error && <p className="text-sm text-red-600">{error}</p>}
          {notice && <p className="text-sm text-emerald-600">{notice}</p>}

          <button
            type="submit"
            disabled={submitting}
            className="mt-1 h-11 w-full rounded-xl bg-ink text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? "请稍候…" : mode === "login" ? "Sign in" : mode === "setup" ? "创建账号" : "注册"}
          </button>
        </form>

        <div className="mt-6 flex flex-col items-center gap-2">
          {(mode !== "login" || accountEntry === "setup") && (
            <button
              type="button"
              onClick={() => {
                setMode(mode === "login" ? "setup" : "login");
                setError(null);
                setNotice(null);
              }}
              className="text-sm text-ink-secondary underline-offset-4 hover:text-ink hover:underline"
            >
              {mode === "login" ? "首次使用？创建账号" : "已有账号？返回登录"}
            </button>
          )}
          {mode === "login" && accountEntry === "register" && (
            <button
              type="button"
              onClick={() => {
                setMode("register");
                setError(null);
                setNotice(null);
              }}
              className="text-sm text-ink-secondary underline-offset-4 hover:text-ink hover:underline"
            >
              没有账号？注册新用户
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
