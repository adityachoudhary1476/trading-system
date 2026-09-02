import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { encryptToken, decryptToken, TokenDecryptionError } from "../crypto";

describe("Upstox token crypto", () => {
  const KEY = "test-key-do-not-use-in-prod-aaaaaaaaaaaaaaaaaa";
  let originalKey: string | undefined;

  beforeAll(() => {
    originalKey = process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;
    process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = KEY;
  });

  afterAll(() => {
    if (originalKey === undefined) {
      delete process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;
    } else {
      process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = originalKey;
    }
  });

  it("round-trips a valid access token", () => {
    const token = "upstox-access-token-XYZ-12345";
    const blob = encryptToken(token);
    expect(typeof blob).toBe("string");
    expect(blob.split(":").length).toBe(3);
    expect(decryptToken(blob)).toBe(token);
  });

  it("produces a different ciphertext each call (random IV)", () => {
    const token = "upstox-access-token-XYZ-12345";
    const blob1 = encryptToken(token);
    const blob2 = encryptToken(token);
    expect(blob1).not.toBe(blob2);
    expect(decryptToken(blob1)).toBe(token);
    expect(decryptToken(blob2)).toBe(token);
  });

  it("rejects a blob encrypted with a different key as TokenDecryptionError", () => {
    const token = "upstox-access-token-XYZ-12345";
    const blob = encryptToken(token);
    process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = "different-key-bbbbbbbbbbbbbbbbbbbbbbbbb";
    try {
      expect(() => decryptToken(blob)).toThrow(TokenDecryptionError);
    } finally {
      process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = KEY;
    }
  });

  it("rejects a malformed blob (wrong segment count)", () => {
    expect(() => decryptToken("not-a-blob")).toThrow(TokenDecryptionError);
    expect(() => decryptToken("only:two")).toThrow(TokenDecryptionError);
  });

  it("rejects a malformed blob (non-base64 segments)", () => {
    expect(() => decryptToken("!!!:@@@:###")).toThrow(TokenDecryptionError);
  });

  it("rejects an empty blob", () => {
    expect(() => decryptToken("")).toThrow(TokenDecryptionError);
  });

  it("rejects a non-string blob", () => {
    // @ts-expect-error – intentionally invalid
    expect(() => decryptToken(null)).toThrow(TokenDecryptionError);
    // @ts-expect-error – intentionally invalid
    expect(() => decryptToken(undefined)).toThrow(TokenDecryptionError);
  });

  it("throws TokenDecryptionError when env key is missing", () => {
    const blob = encryptToken("abc");
    delete process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;
    try {
      expect(() => decryptToken(blob)).toThrow(TokenDecryptionError);
    } finally {
      process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = KEY;
    }
  });

  it("TokenDecryptionError has a stable code for frontend mapping", () => {
    const e = new TokenDecryptionError("x");
    expect(e.code).toBe("token_decryption_failed");
    expect(e.name).toBe("TokenDecryptionError");
  });
});
