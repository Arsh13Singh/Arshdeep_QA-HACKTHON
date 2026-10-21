// DEV ONLY: minimal react-router-dom v6 subset (the 10 APIs DemoBank uses) so the real frontend
// source can be bundled with esbuild where npm is unreachable. Not used by the QA agent.
import {
  Children, startTransition, createContext, isValidElement, useCallback, useContext, useEffect, useMemo, useState,
  type MouseEvent, type ReactElement, type ReactNode,
} from "react";

type Loc = { pathname: string; search: string; state: unknown; key: string };
type NavOpts = { replace?: boolean; state?: unknown };
type Nav = (to: string, opts?: NavOpts) => void;

const RouterCtx = createContext<{ location: Loc; navigate: Nav }>(null as never);
const OutletCtx = createContext<{ chain: ReactElement[]; params: Record<string, string> }>({ chain: [], params: {} });

function readLoc(): Loc {
  const h = window.history.state || {};
  return { pathname: window.location.pathname, search: window.location.search, state: h.usr ?? null, key: h.key ?? "default" };
}

export function BrowserRouter({ children }: { children: ReactNode; future?: unknown }) {
  const [location, setLocation] = useState<Loc>(readLoc);
  useEffect(() => {
    const onPop = () => setLocation(readLoc());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback<Nav>((to, opts = {}) => {
    const url = new URL(to, window.location.origin + window.location.pathname);
    const st = { usr: opts.state ?? null, key: Math.random().toString(36).slice(2, 10) };
    if (opts.replace) window.history.replaceState(st, "", url.pathname + url.search);
    else window.history.pushState(st, "", url.pathname + url.search);
    // Like react-router's v7_startTransition flag: the URL changes now, the screen updates later.
    startTransition(() => setLocation(readLoc()));
  }, []);
  const value = useMemo(() => ({ location, navigate }), [location, navigate]);
  return <RouterCtx.Provider value={value}>{children}</RouterCtx.Provider>;
}

export function Route(_props: { path?: string; element?: ReactNode; children?: ReactNode }): null {
  return null;
}

function matchPath(pattern: string, pathname: string): Record<string, string> | null {
  if (pattern === "*") return {};
  const ps = pattern.split("/").filter(Boolean);
  const xs = pathname.split("/").filter(Boolean);
  if (ps.length !== xs.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < ps.length; i++) {
    if (ps[i].startsWith(":")) params[ps[i].slice(1)] = decodeURIComponent(xs[i]);
    else if (ps[i] !== xs[i]) return null;
  }
  return params;
}

function resolve(children: ReactNode, pathname: string): { chain: ReactElement[]; params: Record<string, string> } | null {
  const routes = Children.toArray(children).filter(isValidElement) as ReactElement<any>[];
  // Static/param routes first, splat last (react-router ranks routes similarly).
  const ranked = [...routes].sort((a, b) => Number(a.props.path === "*") - Number(b.props.path === "*"));
  for (const r of ranked) {
    const { path, element, children: kids } = r.props;
    if (path === undefined) {
      const inner = resolve(kids, pathname);
      if (inner) return { chain: [element, ...inner.chain], params: inner.params };
    } else {
      const params = matchPath(path, pathname);
      if (params) return { chain: [element], params };
    }
  }
  return null;
}

export function Routes({ children }: { children: ReactNode }) {
  const { location } = useContext(RouterCtx);
  const m = resolve(children, location.pathname);
  if (!m) return null;
  return <OutletCtx.Provider value={{ chain: m.chain.slice(1), params: m.params }}>{m.chain[0]}</OutletCtx.Provider>;
}

export function Outlet() {
  const { chain, params } = useContext(OutletCtx);
  if (!chain.length) return null;
  return <OutletCtx.Provider value={{ chain: chain.slice(1), params }}>{chain[0]}</OutletCtx.Provider>;
}

export const useLocation = () => useContext(RouterCtx).location;
export const useNavigate = () => useContext(RouterCtx).navigate;
export const useParams = () => useContext(OutletCtx).params;

export function useSearchParams(): [URLSearchParams, (next: URLSearchParams) => void] {
  const { location, navigate } = useContext(RouterCtx);
  const params = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const set = useCallback((next: URLSearchParams) => {
    const qs = next.toString();
    navigate(location.pathname + (qs ? `?${qs}` : ""));
  }, [location.pathname, navigate]);
  return [params, set];
}

export function Navigate({ to, replace, state }: { to: string; replace?: boolean; state?: unknown }) {
  const navigate = useNavigate();
  useEffect(() => navigate(to, { replace, state }), []); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

type LinkProps = { to: string; children?: ReactNode; className?: string; [k: string]: unknown };

export function Link({ to, children, ...rest }: LinkProps) {
  const navigate = useNavigate();
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigate(to);
  };
  return <a href={to} onClick={onClick} {...rest}>{children}</a>;
}

export function NavLink({ to, className, children, ...rest }: Omit<LinkProps, "className"> & {
  className?: string | ((s: { isActive: boolean }) => string);
}) {
  const { location } = useContext(RouterCtx);
  const isActive = location.pathname === to || location.pathname.startsWith(to + "/");
  const cls = typeof className === "function" ? className({ isActive }) : [className, isActive ? "active" : ""].filter(Boolean).join(" ");
  return <Link to={to} className={cls} aria-current={isActive ? "page" : undefined} {...rest}>{children}</Link>;
}
