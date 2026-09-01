// @vitest-environment node
import { describe, it, expect, beforeEach, afterEach } from "vitest";

const TEST_KEY = "test-key-that-is-exactly-32-bytes!!";

async function loadCrypto() {
  process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = TEST_KEY;
  return import("../../../api/lib/crypto");
}

describe("crypto: encryptToken / decryptToken", () => {
  const originalKey = process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;

  beforeEach(() => {
    process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = TEST_KEY;
  });

  afterEach(() => {
    if (originalKey === undefined) delete process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;
    else process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = originalKey;
  });

  it("round-trips a token", async () => {
    const { encryptToken, decryptToken } = await loadCrypto();
    const token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.test";
    const blob = encryptToken(token);
    expect(decryptToken(blob)).toBe(token);
  });

  it("produces a different ciphertext each time (random IV)", async () => {
    const { encryptToken } = await loadCrypto();
    const token = "same-token-value";
    const a = encryptToken(token);
    const b = encryptToken(token);
    expect(a).not.toBe(b);
  });

  it("throws when encryption key is missing", async () => {
    process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = TEST_KEY;
    const { encryptToken } = await loadCrypto();
    delete process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;
    expect(() => encryptToken("x")).toThrow();
  });

  it("throws on malformed blob", async () => {
    const { decryptToken } = await loadCrypto();
    expect(() => decryptToken("not-a-valid-blob")).toThrow();
  });
});
