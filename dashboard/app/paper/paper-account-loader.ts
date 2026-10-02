import type {
  Account,
  CurrentUser,
  PaperFill,
  PaperOrder,
  PaperOverlaySnapshot,
  PaperPosition,
} from "./types";

type PaperAccountResponse = { account: Account; positions: PaperPosition[] };
type PaperOrdersResponse = { orders: PaperOrder[] };
type PaperFillsResponse = { fills: PaperFill[] };

export type PaperAccountData = PaperOverlaySnapshot & {
  user: CurrentUser;
  account: Account | null;
};

export type PaperApiRequest = <T>(
  path: string,
  init?: RequestInit,
  fallbackMessage?: string,
) => Promise<T>;

export async function loadPaperAccountData(
  request: PaperApiRequest,
): Promise<PaperAccountData> {
  const user = await request<CurrentUser>(
    "/api/me",
    { cache: "no-store" },
    "API 錯誤",
  );
  if (!user.permissions.includes("positions.read.own")) {
    return { user, account: null, positions: [], orders: [], fills: [] };
  }
  const [accountBody, ordersBody, fillsBody] = await Promise.all([
    request<PaperAccountResponse>(
      "/api/paper/account",
      { cache: "no-store" },
      "API 錯誤",
    ),
    request<PaperOrdersResponse>(
      "/api/paper/orders",
      { cache: "no-store" },
      "API 錯誤",
    ),
    request<PaperFillsResponse>(
      "/api/paper/fills?limit=100",
      { cache: "no-store" },
      "API 錯誤",
    ),
  ]);
  return {
    user,
    account: accountBody.account,
    positions: accountBody.positions,
    orders: ordersBody.orders,
    fills: fillsBody.fills,
  };
}
