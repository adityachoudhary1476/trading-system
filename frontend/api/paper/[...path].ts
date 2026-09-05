import type { VercelRequest, VercelResponse } from "@vercel/node";
import { Readable } from "stream";

const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || "http://localhost:8000";

const PROXIED_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);

function getBearer(req: VercelRequest): string | null {
  const value = req.headers.authorization;
  return value?.startsWith("Bearer ") ? value.slice(7).trim() : null;
}

function extractPathAndQuery(req: VercelRequest): { path: string; search: string } {
  const url = req.url || "/";
  const qIdx = url.indexOf("?");
  if (qIdx === -1) {
    return { path: url, search: "" };
  }
  return { path: url.slice(0, qIdx), search: url.slice(qIdx) };
}

function shouldForwardBody(method: string | undefined): boolean {
  return method != null && PROXIED_METHODS.has(method) && method !== "GET" && method !== "DELETE";
}

function buildRequestBody(req: VercelRequest): string | Buffer | undefined {
  const body = req.body;
  if (body == null) return undefined;
  if (typeof body === "string") return body;
  if (typeof body === "object" && !(body instanceof Buffer)) return JSON.stringify(body);
  if (Buffer.isBuffer(body)) return body;
  return undefined;
}

function filterHeaders(req: VercelRequest): Record<string, string> {
  const headers: Record<string, string> = {};
  const contentType = req.headers["content-type"];
  if (contentType) headers["Content-Type"] = Array.isArray(contentType) ? contentType[0] : contentType;
  const accept = req.headers.accept;
  if (accept) headers["Accept"] = Array.isArray(accept) ? accept[0] : accept;
  const bearer = getBearer(req);
  if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
  return headers;
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (!req.method || !PROXIED_METHODS.has(req.method)) {
    res.setHeader("Allow", Array.from(PROXIED_METHODS).join(", "));
    res.status(405).json({ error: "method_not_allowed" });
    return;
  }

  const { path, search } = extractPathAndQuery(req);
  const backendUrl = `${PYTHON_BACKEND_URL}${path}${search}`;
  const headers = filterHeaders(req);
  const body = shouldForwardBody(req.method) ? buildRequestBody(req) : undefined;

  let resp: Response;
  try {
    resp = await fetch(backendUrl, {
      method: req.method,
      headers,
      body,
    });
  } catch {
    res.status(502).json({ error: "backend_unavailable" });
    return;
  }

  const contentType = resp.headers.get("content-type") || "application/json";
  res.setHeader("Content-Type", contentType);
  res.setHeader("Cache-Control", "no-store");

  const bodyText = await resp.text();
  res.status(resp.status).send(bodyText);
}
