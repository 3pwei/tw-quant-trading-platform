"use client";

import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useState,
} from "react";

export type CurrentUser = {
  email: string;
  role: string;
  permissions: string[];
};

export type CurrentUserState =
  | { status: "loading"; user: null }
  | { status: "ready"; user: CurrentUser | null };

const CurrentUserContext = createContext<CurrentUserState | null>(null);

export function CurrentUserProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<CurrentUserState>({
    status: "loading",
    user: null,
  });

  useEffect(() => {
    const controller = new AbortController();

    fetch("/api/me", { cache: "no-store", signal: controller.signal })
      .then(response => response.ok ? response.json() : Promise.reject())
      .then(user => {
        try { sessionStorage.setItem("tmf-demo-nav-authenticated", "1"); } catch { /* Storage may be unavailable. */ }
        setState({ status: "ready", user });
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        try { sessionStorage.removeItem("tmf-demo-nav-authenticated"); } catch { /* Storage may be unavailable. */ }
        setState({ status: "ready", user: null });
      });

    return () => controller.abort();
  }, []);

  return (
    <CurrentUserContext.Provider value={state}>
      {children}
    </CurrentUserContext.Provider>
  );
}

export function useCurrentUser(): CurrentUserState {
  const state = useContext(CurrentUserContext);
  if (!state) {
    throw new Error("useCurrentUser must be used within CurrentUserProvider");
  }
  return state;
}
