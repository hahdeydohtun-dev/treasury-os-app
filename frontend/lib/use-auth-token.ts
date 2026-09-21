"use client";

import { useEffect, useState } from "react";

const TOKEN_KEY = "treasury_os_access_token";

/**
 * Reads the JWT stored by the login page. Returns undefined until the
 * client has mounted (avoids SSR/hydration mismatches) and null if the
 * user isn't signed in.
 */
export function useAuthToken(): string | null | undefined {
  const [token, setToken] = useState<string | null | undefined>(undefined);

  useEffect(() => {
    setToken(window.localStorage.getItem(TOKEN_KEY));
  }, []);

  return token;
}
