import assert from "node:assert/strict";
import test from "node:test";
import type { CurrentUserState } from "../app/components/current-user-context.tsx";
import { routeVisibility } from "../app/components/system-nav-policy.ts";

const loading: CurrentUserState = { status: "loading", user: null };

const admin: CurrentUserState = {
  status: "ready",
  user: {
    email: "admin@example.com",
    role: "admin",
    permissions: ["admin.settings.read"],
  },
};

test("protected routes reserve their space while the user is loading", () => {
  assert.equal(routeVisibility({ admin: true }, loading), "placeholder");
  assert.equal(
    routeVisibility({ permission: "admin.settings.read" }, loading),
    "placeholder",
  );
  assert.equal(routeVisibility({}, loading), "link");
});

test("admin routes remain visible after the shared user state resolves", () => {
  assert.equal(routeVisibility({ admin: true }, admin), "link");
  assert.equal(
    routeVisibility({ permission: "admin.settings.read" }, admin),
    "link",
  );
});

test("protected routes stay hidden from users without access", () => {
  const user: CurrentUserState = {
    status: "ready",
    user: { email: "user@example.com", role: "user", permissions: [] },
  };

  assert.equal(routeVisibility({ admin: true }, user), "hidden");
  assert.equal(
    routeVisibility({ permission: "admin.settings.read" }, user),
    "hidden",
  );
});
