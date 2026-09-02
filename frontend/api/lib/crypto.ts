import { createCipheriv, createDecipheriv, randomBytes, scryptSync } from "node:crypto";

const ALGORITHM = "aes-256-gcm";
const IV_LENGTH = 16;
const TAG_LENGTH = 16;
const KEY_LENGTH = 32;

/**
 * Thrown when the stored Upstox access-token blob cannot be decrypted with the
 * currently configured ``UPSTOX_TOKEN_ENCRYPTION_KEY``.
 *
 * This happens when the key was rotated but the user has not yet reconnected
 * Upstox so that a freshly issued token can be re-encrypted with the current
 * key. Callers MUST surface a clear, non-500 response — the user must
 * reconnect to recover.
 */
export class TokenDecryptionError extends Error {
  readonly code = "token_decryption_failed";
  constructor(message: string) {
    super(message);
    this.name = "TokenDecryptionError";
  }
}

function deriveKey(): Buffer {
  const secret = process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;
  if (!secret) {
    throw new TokenDecryptionError(
      "UPSTOX_TOKEN_ENCRYPTION_KEY is not set; cannot decrypt stored Upstox token.",
    );
  }
  return scryptSync(secret, "finova-upstox-token-v1", KEY_LENGTH);
}

export function encryptToken(plaintext: string): string {
  const key = deriveKey();
  const iv = randomBytes(IV_LENGTH);
  const cipher = createCipheriv(ALGORITHM, key, iv);
  const ciphertext = Buffer.concat([
    cipher.update(plaintext, "utf8"),
    cipher.final(),
  ]);
  const tag = cipher.getAuthTag();
  return [iv.toString("base64"), tag.toString("base64"), ciphertext.toString("base64")].join(":");
}

export function decryptToken(blob: string): string {
  if (typeof blob !== "string" || blob.length === 0) {
    throw new TokenDecryptionError("Stored Upstox token is missing or empty.");
  }
  const parts = blob.split(":");
  if (parts.length !== 3) {
    throw new TokenDecryptionError("Stored Upstox token has an unexpected format.");
  }
  const [ivB64, tagB64, ctB64] = parts;
  if (!ivB64 || !tagB64 || !ctB64) {
    throw new TokenDecryptionError("Stored Upstox token has an unexpected format.");
  }
  let iv: Buffer;
  let tag: Buffer;
  let ciphertext: Buffer;
  try {
    iv = Buffer.from(ivB64, "base64");
    tag = Buffer.from(tagB64, "base64");
    ciphertext = Buffer.from(ctB64, "base64");
  } catch {
    throw new TokenDecryptionError("Stored Upstox token has an unexpected format.");
  }
  if (iv.length !== IV_LENGTH || tag.length !== TAG_LENGTH || ciphertext.length === 0) {
    throw new TokenDecryptionError("Stored Upstox token has an unexpected format.");
  }
  const key = deriveKey();
  let plaintext: Buffer;
  try {
    const decipher = createDecipheriv(ALGORITHM, key, iv);
    decipher.setAuthTag(tag);
    plaintext = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
  } catch {
    // GCM auth-tag mismatch or other crypto failure ⇒ the stored token was
    // encrypted with a different key. The user must reconnect Upstox.
    throw new TokenDecryptionError(
      "Stored Upstox token cannot be decrypted with the current encryption key. " +
        "Reconnect Upstox to issue a new access token.",
    );
  }
  return plaintext.toString("utf8");
}

export const CONSTANT_TIME_TAG_LENGTH = TAG_LENGTH;
