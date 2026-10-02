import type { CurrentUserState } from "./current-user-context";

export type ProtectedRoute = {
  permission?: string;
  admin?: boolean;
};

export type RouteVisibility = "link" | "placeholder" | "hidden";

export function routeVisibility(
  route: ProtectedRoute,
  currentUser: CurrentUserState,
): RouteVisibility {
  const isProtected = Boolean(route.admin || route.permission);
  if (currentUser.status === "loading") {
    return isProtected ? "placeholder" : "link";
  }

  if (route.admin && currentUser.user?.role !== "admin") return "hidden";
  if (
    route.permission
    && !currentUser.user?.permissions.includes(route.permission)
  ) {
    return "hidden";
  }
  return "link";
}
