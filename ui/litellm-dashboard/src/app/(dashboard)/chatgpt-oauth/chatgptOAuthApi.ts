import { getGlobalLitellmHeaderName, getProxyBaseUrl } from "@/components/networking";
import { createApiClient } from "@/lib/http/client";

const client = createApiClient({
  getBaseUrl: getProxyBaseUrl,
  getAuthHeaderName: getGlobalLitellmHeaderName,
});

export const chatgptOAuthApi = {
  get: <T>(path: string, accessToken: string): Promise<T> => client.get<T>(path, { accessToken }),
  post: <T>(path: string, accessToken: string, body?: unknown): Promise<T> =>
    client.post<T>(path, { accessToken, body }),
  patch: <T>(path: string, accessToken: string, body: unknown): Promise<T> =>
    client.patch<T>(path, { accessToken, body }),
};
